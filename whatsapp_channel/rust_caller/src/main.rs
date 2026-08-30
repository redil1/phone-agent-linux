use std::env;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use anyhow::{Context, Result, anyhow, bail};
use async_channel::{Receiver, Sender, TryRecvError, TrySendError};
use bytes::Bytes;
use serde::Deserialize;
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWriteExt};
use tokio::sync::watch;
use whatsapp_rust::pair_code::PairCodeOptions;
use whatsapp_rust::prelude::*;
use whatsapp_rust::voip::audio::{WaOpusDecoder, WaOpusEncoder};
use whatsapp_rust::voip::{AudioCodec, AudioFormat, CallTermination, EncodedAudioFrame};
use whatsapp_rust::wacore::types::call::CallAction;
use whatsapp_rust::wacore::types::events::Event;
use whatsapp_rust::wacore::voip::CallEvent;

const SAMPLE_RATE: usize = 16_000;
const FRAME_MS: usize = 60;
const FRAME_SAMPLES: usize = SAMPLE_RATE * FRAME_MS / 1000;
const FRAME_BYTES: usize = FRAME_SAMPLES * 2;
const FRAME_DURATION: Duration = Duration::from_millis(FRAME_MS as u64);
const ANSWER_TIMEOUT_SECS: u64 = 60;
const CONNECT_TIMEOUT_SECS: u64 = 45;
const PAIR_TIMEOUT_SECS: u64 = 180;

// Python -> Rust framed stdin. Peer PCM remains raw on stdout so logs can never
// corrupt audio and the receive side stays as small as possible.
const BRIDGE_MAGIC: &[u8; 4] = b"WAR1";
const BRIDGE_HEADER_BYTES: usize = 25;
const BRIDGE_KIND_AUDIO: u8 = 1;
const BRIDGE_KIND_CONTROL: u8 = 2;
const BRIDGE_MAX_PAYLOAD: usize = 64 * 1024;

#[derive(Debug)]
struct Cli {
    command: String,
    positional: Vec<String>,
    country_code: String,
    duration_secs: u64,
    framed_stdio: bool,
    session_db: PathBuf,
}

#[derive(Debug, Clone)]
enum SignalKind {
    Ringing,
    Accepted,
    Rejected(Option<String>),
    Terminated(Option<String>),
}

#[derive(Debug, Clone)]
struct CallSignal {
    call_id: String,
    kind: SignalKind,
}

#[derive(Debug)]
struct BridgeFrame {
    kind: u8,
    generation: u64,
    sequence: u64,
    payload: Vec<u8>,
}

type NativeOpusBridge = (
    Receiver<Bytes>,
    Sender<EncodedAudioFrame>,
    Vec<tokio::task::JoinHandle<()>>,
);

#[derive(Debug, Deserialize)]
struct ControlMessage {
    #[serde(rename = "type")]
    kind: String,
    #[serde(default)]
    next_generation: Option<u64>,
    #[serde(default)]
    generation: Option<u64>,
    #[serde(default)]
    sequence: Option<u64>,
    #[serde(default)]
    reason: Option<String>,
}

fn main() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("warn"))
        .target(env_logger::Target::Stderr)
        .init();

    let cli = match parse_cli() {
        Ok(cli) => cli,
        Err(error) => {
            eprintln!("Error: {error:#}");
            std::process::exit(2);
        }
    };

    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("failed to build Tokio runtime");
    if let Err(error) = runtime.block_on(run(cli)) {
        eprintln!("Error: {error:#}");
        std::process::exit(1);
    }
}

async fn run(cli: Cli) -> Result<()> {
    ensure_session_parent(&cli.session_db)?;
    match cli.command.as_str() {
        "status" => status(cli.session_db).await,
        "pair-phone" => {
            let phone = cli
                .positional
                .first()
                .ok_or_else(|| anyhow!("pair-phone requires your WhatsApp phone number"))?;
            pair_phone(phone, &cli.country_code, cli.session_db).await
        }
        "call" => {
            let target = cli
                .positional
                .first()
                .ok_or_else(|| anyhow!("call requires a target phone number"))?;
            if !cli.framed_stdio {
                bail!("the PhoneAgent integration requires --framed-stdio");
            }
            call(target, &cli.country_code, cli.duration_secs, cli.session_db).await
        }
        "resolve-target" => {
            let target = cli
                .positional
                .first()
                .ok_or_else(|| anyhow!("resolve-target requires a target phone number"))?;
            resolve_target_command(target, &cli.country_code, cli.session_db).await
        }
        _ => bail!(
            "unknown command {:?}; use status, pair-phone, resolve-target, or call",
            cli.command
        ),
    }
}

fn parse_cli() -> Result<Cli> {
    let mut args = env::args().skip(1);
    let command = args.next().unwrap_or_else(|| "help".to_string());
    let mut positional = Vec::new();
    let mut country_code = "212".to_string();
    let mut duration_secs = 900u64;
    let mut framed_stdio = false;
    let mut session_db = default_session_db();

    let remaining: Vec<String> = args.collect();
    let mut index = 0;
    while index < remaining.len() {
        match remaining[index].as_str() {
            "--country-code" => {
                index += 1;
                country_code = remaining
                    .get(index)
                    .context("--country-code requires a value")?
                    .clone();
            }
            "--duration" => {
                index += 1;
                duration_secs = remaining
                    .get(index)
                    .context("--duration requires a value")?
                    .parse()
                    .context("--duration must be an integer number of seconds")?;
            }
            "--session-db" => {
                index += 1;
                session_db = PathBuf::from(
                    remaining
                        .get(index)
                        .context("--session-db requires a path")?,
                );
            }
            "--framed-stdio" => framed_stdio = true,
            value if value.starts_with('-') => bail!("unknown option {value}"),
            value => positional.push(value.to_string()),
        }
        index += 1;
    }
    duration_secs = duration_secs.clamp(30, 3600);
    Ok(Cli {
        command,
        positional,
        country_code,
        duration_secs,
        framed_stdio,
        session_db,
    })
}

fn default_session_db() -> PathBuf {
    if let Some(path) = env::var_os("PHONE_AGENT_WHATSAPP_SESSION_DB") {
        return PathBuf::from(path);
    }
    let base = env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    base.join(".local/share/phone-agent/whatsapp-rust.db")
}

fn ensure_session_parent(path: &Path) -> Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .with_context(|| format!("could not create {}", parent.display()))?;
    }
    Ok(())
}

async fn build_bot(db: &Path, signals: Sender<CallSignal>) -> Result<whatsapp_rust::bot::Bot> {
    let store = SqliteStore::new(db.to_string_lossy().as_ref())
        .await
        .with_context(|| format!("could not open WhatsApp session {}", db.display()))?;
    let bot = Bot::builder()
        .with_backend(store)
        .skip_history_sync()
        .with_event_delivery(EventDelivery::Ordered { capacity: 256 })
        .on_event(move |event, _client| {
            let signals = signals.clone();
            async move {
                let Event::IncomingCall(call) = &*event else {
                    return;
                };
                let kind = match &call.action {
                    CallAction::PreAccept { .. } => SignalKind::Ringing,
                    CallAction::Accept { .. } => SignalKind::Accepted,
                    CallAction::Reject { reason, .. } => SignalKind::Rejected(reason.clone()),
                    CallAction::Terminate { reason, .. } => SignalKind::Terminated(reason.clone()),
                    _ => return,
                };
                let _ = signals
                    .send(CallSignal {
                        call_id: call.action.call_id().to_string(),
                        kind,
                    })
                    .await;
            }
        })
        .build()
        .await?;
    Ok(bot)
}

async fn status(db: PathBuf) -> Result<()> {
    let (tx, _rx) = async_channel::bounded(1);
    let bot = build_bot(&db, tx).await?;
    let client = bot.client();
    if let Some(pn) = client.pn() {
        println!("status: logged_in");
        println!("jid: {pn}");
        println!("push_name: {}", client.push_name());
    } else {
        println!("status: not_logged_in");
    }
    Ok(())
}

async fn pair_phone(raw_phone: &str, country_code: &str, db: PathBuf) -> Result<()> {
    let phone = normalize_number(raw_phone, country_code);
    let store = SqliteStore::new(db.to_string_lossy().as_ref()).await?;
    let bot = Bot::builder()
        .with_backend(store)
        .skip_history_sync()
        .with_pair_code(PairCodeOptions {
            phone_number: phone,
            ..Default::default()
        })
        .on_pair_code(|code, _timeout| async move {
            println!("YOUR PAIRING CODE: {code}");
        })
        .on_pair_code_error(|error, _client| async move {
            eprintln!("PAIRING_ERROR {error:?}");
        })
        .on_qr_code(|_code, timeout| async move {
            eprintln!(
                "QR pairing is also available for {} seconds; the Studio uses the phone code",
                timeout.as_secs()
            );
        })
        .build()
        .await?;
    if bot.client().pn().is_some() {
        println!("Pairing complete! Session already linked.");
        return Ok(());
    }
    let handle = bot.spawn();
    let client = handle.client();
    client
        .wait_for_connected(Duration::from_secs(PAIR_TIMEOUT_SECS))
        .await
        .context("pairing did not complete before the code expired")?;
    println!("Pairing complete! Session saved.");
    handle.shutdown().await;
    Ok(())
}

/// Resolve a phone-number JID through WhatsApp's interactive contact usync.
///
/// VoIP media keys are derived from the callee's LID, not their phone-number
/// JID. A newly paired companion often has no contacts/history yet, so the
/// ordinary device-list lookup has no PN -> LID cache entry to reuse. The
/// contact existence query returns that mapping and the library persists it.
async fn resolve_target_lid(client: &Client, target_pn: &Jid) -> Result<Jid> {
    if let Some(entry) = client
        .get_lid_pn_entry(target_pn)
        .await
        .context("could not read the WhatsApp LID cache")?
    {
        return Ok(Jid::lid(entry.lid.as_ref()));
    }

    let results = client
        .contacts()
        .is_on_whatsapp(std::slice::from_ref(target_pn))
        .await
        .context("WhatsApp contact lookup failed")?;
    let result = results
        .into_iter()
        .next()
        .context("WhatsApp returned no contact result for the target")?;
    if !result.is_registered {
        bail!("target is not registered on WhatsApp");
    }

    if let Some(lid) = result.lid.filter(Jid::is_lid) {
        return Ok(lid.to_non_ad());
    }
    if result.jid.is_lid() {
        return Ok(result.jid.to_non_ad());
    }
    if let Some(entry) = client
        .get_lid_pn_entry(target_pn)
        .await
        .context("could not read the WhatsApp LID learned for the target")?
    {
        return Ok(Jid::lid(entry.lid.as_ref()));
    }

    bail!("target is registered on WhatsApp, but its LID could not be resolved")
}

async fn resolve_target_command(raw_target: &str, country_code: &str, db: PathBuf) -> Result<()> {
    let (signal_tx, _signal_rx) = async_channel::unbounded::<CallSignal>();
    let bot = build_bot(&db, signal_tx).await?;
    let bot_handle = bot.spawn();
    let client = bot_handle.client();
    client
        .wait_for_connected(Duration::from_secs(CONNECT_TIMEOUT_SECS))
        .await
        .context("WhatsApp session did not become ready")?;
    if client.pn().is_none() {
        bot_handle.shutdown().await;
        bail!("not paired; pair this machine before resolving a target");
    }

    let target_digits = normalize_number(raw_target, country_code);
    let target_pn = Jid::pn(target_digits);
    let target_lid = resolve_target_lid(&client, &target_pn).await?;
    println!("status: registered");
    println!("pn: {target_pn}");
    println!("lid: {target_lid}");
    bot_handle.shutdown().await;
    Ok(())
}

async fn call(raw_target: &str, country_code: &str, duration_secs: u64, db: PathBuf) -> Result<()> {
    if env::var_os("PHONE_AGENT_WHATSAPP_RUST_MOCK").is_some() {
        return mock_call(raw_target, duration_secs).await;
    }
    let (signal_tx, signal_rx) = async_channel::unbounded::<CallSignal>();
    let bot = build_bot(&db, signal_tx).await?;
    let bot_handle = bot.spawn();
    let client = bot_handle.client();
    client
        .wait_for_connected(Duration::from_secs(CONNECT_TIMEOUT_SECS))
        .await
        .context("WhatsApp session did not become ready")?;
    if client.pn().is_none() {
        bot_handle.shutdown().await;
        bail!("not paired; pair this machine before placing a call");
    }

    let target_digits = normalize_number(raw_target, country_code);
    let target_pn = Jid::pn(target_digits);
    let target = resolve_target_lid(&client, &target_pn)
        .await
        .context("failed to resolve WhatsApp call target")?;
    eprintln!("TARGET_RESOLVED pn={target_pn} lid={target}");
    let (mic_tx, mic_rx) = async_channel::bounded::<Vec<i16>>(2);
    let mic_drain = mic_rx.clone();
    let (speaker_tx, speaker_rx) = async_channel::bounded::<Vec<i16>>(8);
    let (encoded_rx, encoded_playout_tx, codec_tasks) =
        spawn_native_opus_bridge(mic_rx, speaker_tx)?;

    let call_result = client
        .voip()
        .call(&target)
        .encoded_audio(AudioFormat::OPUS_16KHZ_60MS, encoded_rx, encoded_playout_tx)
        .start()
        .await;
    let call_handle = match call_result {
        Ok(handle) => handle,
        Err(error) => {
            for task in codec_tasks {
                abort_join(task).await;
            }
            return Err(error).context("failed to place WhatsApp call");
        }
    };
    let call_id = call_handle.call_id().to_string();
    eprintln!("[+] Call placed! Call ID: {call_id}");
    eprintln!("[*] Ringing target device...");
    eprintln!("AUDIO_CODEC native_opus_pt120 sample_rate=16000 frame_ms=60");

    let active_generation = Arc::new(AtomicU64::new(1));
    let (hangup_tx, mut hangup_rx) = watch::channel(false);
    let stdin_task = tokio::spawn(read_agent_input(
        mic_tx,
        mic_drain,
        active_generation.clone(),
        hangup_tx,
    ));
    let stdout_task = tokio::spawn(write_peer_audio(speaker_rx));
    let diagnostic_task = tokio::spawn(report_call_events(call_handle.events(), call_id.clone()));
    let ended_handle = call_handle.clone();
    let mut ended_task = tokio::spawn(async move { ended_handle.wait_ended().await });

    let answered = wait_for_answer(
        &call_id,
        &signal_rx,
        &mut ended_task,
        Duration::from_secs(ANSWER_TIMEOUT_SECS),
    )
    .await?;
    if !answered {
        let _ = call_handle.terminate().await;
        abort_join(stdin_task).await;
        abort_join(stdout_task).await;
        abort_join(diagnostic_task).await;
        for task in codec_tasks {
            abort_join(task).await;
        }
        bot_handle.shutdown().await;
        bail!("the WhatsApp call was not answered");
    }
    eprintln!("[+] Peer ACCEPTED the call! Audio channel is active.");

    enum EndCause {
        Peer,
        Local(&'static str),
    }
    let duration = tokio::time::sleep(Duration::from_secs(duration_secs));
    tokio::pin!(duration);
    let cause = tokio::select! {
        _ = &mut ended_task => EndCause::Peer,
        changed = hangup_rx.changed() => {
            let reason = if changed.is_ok() && *hangup_rx.borrow() {
                "agent_requested"
            } else {
                "agent_pipe_closed"
            };
            EndCause::Local(reason)
        }
        _ = &mut duration => EndCause::Local("duration_limit"),
        _ = termination_signal() => EndCause::Local("process_signal"),
    };

    match cause {
        EndCause::Peer => eprintln!("[-] Call ended by peer"),
        EndCause::Local(reason) => {
            let outcome = call_handle.terminate().await;
            report_termination(reason, &outcome);
        }
    }

    abort_join(stdin_task).await;
    abort_join(stdout_task).await;
    abort_join(diagnostic_task).await;
    for task in codec_tasks {
        abort_join(task).await;
    }
    if !ended_task.is_finished() {
        ended_task.abort();
    }
    let _ = ended_task.await;
    eprintln!("[-] Call ended: completed");
    bot_handle.shutdown().await;
    Ok(())
}

fn spawn_native_opus_bridge(
    mic: Receiver<Vec<i16>>,
    speaker: Sender<Vec<i16>>,
) -> Result<NativeOpusBridge> {
    let mut encoder = WaOpusEncoder::new().context("could not initialize Opus encoder")?;
    let mut decoder = WaOpusDecoder::new().context("could not initialize Opus decoder")?;
    let (encoded_tx, encoded_rx) = async_channel::bounded::<Bytes>(3);
    let (playout_tx, playout_rx) = async_channel::bounded::<EncodedAudioFrame>(3);

    let encode_task = tokio::spawn(async move {
        let silence = vec![0i16; FRAME_SAMPLES];
        let mut idle_frames = 0u64;
        let mut cadence = tokio::time::interval(FRAME_DURATION);
        cadence.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        // interval() ticks immediately; consume that tick so the first encoded
        // frame is paced one full WhatsApp packet after media setup.
        cadence.tick().await;
        loop {
            cadence.tick().await;
            let pcm = match mic.try_recv() {
                Ok(pcm) => Some(pcm),
                Err(TryRecvError::Empty) => {
                    idle_frames += 1;
                    if idle_frames.is_multiple_of(100) {
                        eprintln!("MEDIA_IDLE_DTX frames={idle_frames}");
                    }
                    None
                }
                Err(TryRecvError::Closed) => break,
            };
            let samples = pcm.as_deref().unwrap_or(&silence);
            match encoder.encode(samples).map(Bytes::from) {
                Ok(payload) => {
                    if encoded_tx.send(payload).await.is_err() {
                        break;
                    }
                }
                Err(error) => eprintln!("MEDIA_ERROR opus_encode={error}"),
            }
        }
    });
    let decode_task = tokio::spawn(async move {
        while let Ok(frame) = playout_rx.recv().await {
            if frame.codec != AudioCodec::Opus {
                eprintln!(
                    "MEDIA_ERROR negotiated_codec={:?} expected=Opus sequence={}",
                    frame.codec, frame.sequence_number
                );
                continue;
            }
            match decoder.decode(&frame.data) {
                Ok(pcm) => {
                    if speaker.send(pcm.to_vec()).await.is_err() {
                        break;
                    }
                }
                Err(error) => eprintln!("MEDIA_ERROR opus_decode={error}"),
            }
        }
    });

    Ok((encoded_rx, playout_tx, vec![encode_task, decode_task]))
}

/// Deterministic local loopback used by cross-language contract tests. It is
/// unreachable unless the explicit test-only environment variable is set.
async fn mock_call(raw_target: &str, duration_secs: u64) -> Result<()> {
    eprintln!("[+] Call placed! Call ID: MOCK-CALL");
    eprintln!("[*] Ringing target device {raw_target}...");
    tokio::time::sleep(Duration::from_millis(10)).await;
    eprintln!("[+] Peer ACCEPTED the call! Audio channel is active.");

    let mut stdin = tokio::io::stdin();
    let mut stdout = tokio::io::stdout();
    let mut generation = 1u64;
    let duration = tokio::time::sleep(Duration::from_secs(duration_secs));
    tokio::pin!(duration);

    loop {
        tokio::select! {
            _ = &mut duration => break,
            frame = read_bridge_frame(&mut stdin) => {
                let Some(frame) = frame? else { break };
                match frame.kind {
                    BRIDGE_KIND_AUDIO if frame.generation == generation => {
                        stdout.write_all(&frame.payload).await?;
                        stdout.flush().await?;
                        eprintln!(
                            "PLAYOUT_ACK generation={} sequence={}",
                            frame.generation, frame.sequence
                        );
                    }
                    BRIDGE_KIND_CONTROL => {
                        let control: ControlMessage = serde_json::from_slice(&frame.payload)?;
                        match control.kind.as_str() {
                            "flush" => {
                                generation = control.next_generation.unwrap_or(frame.generation);
                                eprintln!("FLUSH_ACK generation={generation}");
                            }
                            "audio_end" => {
                                eprintln!(
                                    "PLAYOUT_ACK generation={} sequence={}",
                                    control.generation.unwrap_or(frame.generation),
                                    control.sequence.unwrap_or(frame.sequence)
                                );
                            }
                            "hangup" => break,
                            _ => {}
                        }
                    }
                    _ => {}
                }
            }
        }
    }
    eprintln!("[-] Call ended: mock_completed");
    Ok(())
}

async fn wait_for_answer(
    call_id: &str,
    signals: &Receiver<CallSignal>,
    ended: &mut tokio::task::JoinHandle<()>,
    timeout: Duration,
) -> Result<bool> {
    let deadline = tokio::time::sleep(timeout);
    tokio::pin!(deadline);
    loop {
        tokio::select! {
            _ = &mut *ended => return Ok(false),
            _ = &mut deadline => return Ok(false),
            received = signals.recv() => {
                let signal = received.context("call signaling stream closed")?;
                if signal.call_id != call_id {
                    continue;
                }
                match signal.kind {
                    SignalKind::Ringing => eprintln!("[*] Call State: RINGING"),
                    SignalKind::Accepted => return Ok(true),
                    SignalKind::Rejected(reason) => {
                        eprintln!("[-] Call rejected: {}", reason.unwrap_or_else(|| "declined".into()));
                        return Ok(false);
                    }
                    SignalKind::Terminated(reason) => {
                        eprintln!("[-] Call terminated: {}", reason.unwrap_or_else(|| "remote".into()));
                        return Ok(false);
                    }
                }
            }
        }
    }
}

async fn report_call_events(events: Receiver<CallEvent>, call_id: String) {
    while let Ok(event) = events.recv().await {
        match event {
            CallEvent::RelayAllocated => eprintln!("MEDIA_READY call_id={call_id}"),
            CallEvent::OutboundMediaDropped {
                video_access_units,
                packets,
            } => eprintln!(
                "MEDIA_DROP call_id={call_id} packets={packets} video_access_units={video_access_units}"
            ),
            CallEvent::AudioSilent {
                silent_for_ms,
                dominant_reason,
                ..
            } => eprintln!(
                "MEDIA_SILENT call_id={call_id} silent_ms={silent_for_ms:?} reason={dominant_reason:?}"
            ),
            CallEvent::AudioReceptionStalled { silent_for_ms } => {
                eprintln!("MEDIA_STALLED call_id={call_id} silent_ms={silent_for_ms:?}")
            }
            CallEvent::AudioCodecSwitched {
                from, to, source, ..
            } => eprintln!(
                "MEDIA_CODEC_SWITCH call_id={call_id} from={from:?} to={to:?} source={source:?}"
            ),
            CallEvent::RelayAllocateFailed(code) => {
                eprintln!("MEDIA_ERROR call_id={call_id} relay_allocate_code={code}")
            }
            CallEvent::RelayAllocateTimedOut | CallEvent::RelayReconnectTimedOut => {
                eprintln!("MEDIA_ERROR call_id={call_id} event={event:?}")
            }
            _ => {}
        }
    }
}

async fn read_agent_input(
    mic_tx: Sender<Vec<i16>>,
    mic_drain: Receiver<Vec<i16>>,
    active_generation: Arc<AtomicU64>,
    hangup_tx: watch::Sender<bool>,
) -> Result<()> {
    let mut stdin = tokio::io::stdin();
    while let Some(frame) = read_bridge_frame(&mut stdin).await? {
        match frame.kind {
            BRIDGE_KIND_AUDIO => {
                if frame.generation != active_generation.load(Ordering::Acquire) {
                    continue;
                }
                if frame.payload.len() != FRAME_BYTES {
                    eprintln!(
                        "MEDIA_DROP generation={} sequence={} reason=bad_frame_bytes bytes={}",
                        frame.generation,
                        frame.sequence,
                        frame.payload.len()
                    );
                    continue;
                }
                let samples = pcm_bytes_to_i16(&frame.payload);
                match mic_tx.try_send(samples) {
                    Ok(()) => {}
                    Err(TrySendError::Full(samples)) => {
                        let _ = mic_drain.try_recv();
                        if mic_tx.try_send(samples).is_err() {
                            eprintln!(
                                "MEDIA_DROP generation={} sequence={} reason=source_backpressure",
                                frame.generation, frame.sequence
                            );
                            continue;
                        }
                    }
                    Err(TrySendError::Closed(_)) => break,
                }
                eprintln!(
                    "PLAYOUT_ACK generation={} sequence={}",
                    frame.generation, frame.sequence
                );
            }
            BRIDGE_KIND_CONTROL => {
                let control: ControlMessage = serde_json::from_slice(&frame.payload)
                    .context("invalid bridge control JSON")?;
                match control.kind.as_str() {
                    "flush" => {
                        let next = control
                            .next_generation
                            .unwrap_or_else(|| frame.generation.max(1));
                        active_generation.store(next, Ordering::Release);
                        drain_audio(&mic_drain);
                        eprintln!("FLUSH_ACK generation={next}");
                    }
                    "audio_end" => {
                        let generation = control.generation.unwrap_or(frame.generation);
                        let sequence = control.sequence.unwrap_or(frame.sequence);
                        let queue = mic_drain.clone();
                        let active = active_generation.clone();
                        tokio::spawn(async move {
                            while !queue.is_empty() && active.load(Ordering::Acquire) == generation
                            {
                                tokio::time::sleep(Duration::from_millis(5)).await;
                            }
                            tokio::time::sleep(Duration::from_millis(FRAME_MS as u64)).await;
                            if active.load(Ordering::Acquire) == generation {
                                eprintln!(
                                    "PLAYOUT_ACK generation={} sequence={}",
                                    generation, sequence
                                );
                            }
                        });
                    }
                    "hangup" => {
                        eprintln!(
                            "HANGUP_REQUEST reason={}",
                            control.reason.unwrap_or_else(|| "agent".into())
                        );
                        let _ = hangup_tx.send(true);
                    }
                    other => eprintln!("CONTROL_IGNORED type={other}"),
                }
            }
            other => eprintln!("CONTROL_IGNORED frame_kind={other}"),
        }
    }
    let _ = hangup_tx.send(true);
    Ok(())
}

async fn read_bridge_frame<R: AsyncRead + Unpin>(reader: &mut R) -> Result<Option<BridgeFrame>> {
    let mut header = [0u8; BRIDGE_HEADER_BYTES];
    match reader.read_exact(&mut header).await {
        Ok(_) => {}
        Err(error) if error.kind() == ErrorKind::UnexpectedEof => return Ok(None),
        Err(error) => return Err(error.into()),
    }
    if &header[0..4] != BRIDGE_MAGIC {
        bail!("invalid bridge frame magic");
    }
    let kind = header[4];
    let generation = u64::from_be_bytes(header[5..13].try_into().unwrap());
    let sequence = u64::from_be_bytes(header[13..21].try_into().unwrap());
    let length = u32::from_be_bytes(header[21..25].try_into().unwrap()) as usize;
    if length > BRIDGE_MAX_PAYLOAD {
        bail!("bridge payload exceeds {BRIDGE_MAX_PAYLOAD} bytes");
    }
    let mut payload = vec![0u8; length];
    reader.read_exact(&mut payload).await?;
    Ok(Some(BridgeFrame {
        kind,
        generation,
        sequence,
        payload,
    }))
}

async fn write_peer_audio(receiver: Receiver<Vec<i16>>) -> Result<()> {
    let mut stdout = tokio::io::stdout();
    while let Ok(samples) = receiver.recv().await {
        let bytes = i16_to_pcm_bytes(&samples);
        stdout.write_all(&bytes).await?;
        stdout.flush().await?;
    }
    Ok(())
}

fn drain_audio(receiver: &Receiver<Vec<i16>>) {
    loop {
        match receiver.try_recv() {
            Ok(_) => {}
            Err(TryRecvError::Empty | TryRecvError::Closed) => return,
        }
    }
}

fn pcm_bytes_to_i16(bytes: &[u8]) -> Vec<i16> {
    bytes
        .chunks_exact(2)
        .map(|chunk| i16::from_le_bytes([chunk[0], chunk[1]]))
        .collect()
}

fn i16_to_pcm_bytes(samples: &[i16]) -> Vec<u8> {
    let mut bytes = Vec::with_capacity(samples.len() * 2);
    for sample in samples {
        bytes.extend_from_slice(&sample.to_le_bytes());
    }
    bytes
}

fn normalize_number(raw: &str, country_code: &str) -> String {
    let mut value: String = raw
        .trim()
        .chars()
        .filter(|ch| ch.is_ascii_digit() || *ch == '+')
        .collect();
    if let Some(rest) = value.strip_prefix('+') {
        return rest.to_string();
    }
    if let Some(rest) = value.strip_prefix("00") {
        return rest.to_string();
    }
    if value.starts_with('0') && value.len() == 10 {
        value.remove(0);
        return format!("{country_code}{value}");
    }
    value
}

fn report_termination(reason: &str, outcome: &CallTermination) {
    eprintln!(
        "[*] Local hangup reason={reason} peer_notified={} outcome={outcome:?}",
        outcome.peer_notified()
    );
}

async fn abort_join<T>(task: tokio::task::JoinHandle<T>) {
    task.abort();
    let _ = task.await;
}

#[cfg(unix)]
async fn termination_signal() {
    use tokio::signal::unix::{SignalKind, signal};
    let mut terminate = signal(SignalKind::terminate()).expect("install SIGTERM handler");
    tokio::select! {
        _ = tokio::signal::ctrl_c() => {},
        _ = terminate.recv() => {},
    }
}

#[cfg(not(unix))]
async fn termination_signal() {
    let _ = tokio::signal::ctrl_c().await;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn number_normalization_matches_the_phone_agent_contract() {
        assert_eq!(normalize_number("0600454425", "212"), "212600454425");
        assert_eq!(normalize_number("+33612345678", "212"), "33612345678");
        assert_eq!(normalize_number("0033612345678", "212"), "33612345678");
    }

    #[test]
    fn pcm_round_trip_is_exact() {
        let samples: Vec<i16> = (-480..480).collect();
        let bytes = i16_to_pcm_bytes(&samples);
        assert_eq!(bytes.len(), FRAME_BYTES);
        assert_eq!(pcm_bytes_to_i16(&bytes), samples);
    }

    #[test]
    fn native_opus_adapter_round_trips_one_whatsapp_frame() {
        let input: Vec<i16> = (0..FRAME_SAMPLES)
            .map(|index| (((index as f32 / 16.0).sin()) * 12_000.0) as i16)
            .collect();
        let mut encoder = WaOpusEncoder::new().unwrap();
        let mut decoder = WaOpusDecoder::new().unwrap();
        let encoded = encoder.encode(&input).unwrap();
        let decoded = decoder.decode(&encoded).unwrap();

        assert_eq!(decoded.len(), FRAME_SAMPLES);
        assert!(decoded.iter().any(|sample| *sample != 0));
    }

    #[tokio::test]
    async fn native_opus_bridge_emits_dtx_while_the_agent_is_listening() {
        let (mic_tx, mic_rx) = async_channel::bounded::<Vec<i16>>(2);
        let (speaker_tx, _speaker_rx) = async_channel::bounded::<Vec<i16>>(2);
        let (encoded_rx, _playout_tx, tasks) =
            spawn_native_opus_bridge(mic_rx, speaker_tx).unwrap();

        let frame = tokio::time::timeout(Duration::from_millis(150), encoded_rx.recv())
            .await
            .expect("the listening side must emit before WhatsApp's media watchdog")
            .unwrap();
        assert!(!frame.is_empty());

        drop(mic_tx);
        for task in tasks {
            abort_join(task).await;
        }
    }

    #[tokio::test]
    async fn bridge_frame_parser_accepts_fragmented_input() {
        let payload = vec![7u8; FRAME_BYTES];
        let mut encoded = Vec::new();
        encoded.extend_from_slice(BRIDGE_MAGIC);
        encoded.push(BRIDGE_KIND_AUDIO);
        encoded.extend_from_slice(&4u64.to_be_bytes());
        encoded.extend_from_slice(&9u64.to_be_bytes());
        encoded.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        encoded.extend_from_slice(&payload);
        let mut reader = tokio::io::BufReader::new(encoded.as_slice());
        let frame = read_bridge_frame(&mut reader).await.unwrap().unwrap();
        assert_eq!(frame.kind, BRIDGE_KIND_AUDIO);
        assert_eq!(frame.generation, 4);
        assert_eq!(frame.sequence, 9);
        assert_eq!(frame.payload, payload);
    }
}
