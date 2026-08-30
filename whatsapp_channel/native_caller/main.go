package main

import (
	"bufio"
	"context"
	"encoding/binary"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/mdp/qrterminal/v3"
	"github.com/purpshell/meowcaller"
	"github.com/rs/zerolog"
	"go.mau.fi/whatsmeow"
	waProto "go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
	_ "modernc.org/sqlite"
)

func normalizeNumber(phone string, defaultCountryCode string) string {
	reg := regexp.MustCompile(`[^\d+]`)
	cleaned := reg.ReplaceAllString(phone, "")
	if strings.HasPrefix(cleaned, "+") {
		return cleaned
	}
	if strings.HasPrefix(cleaned, "00") {
		return "+" + cleaned[2:]
	}
	if strings.HasPrefix(cleaned, "0") && len(cleaned) == 10 {
		return "+" + defaultCountryCode + cleaned[1:]
	}
	return "+" + cleaned
}

func normalizeNumberDigitsOnly(phone string, defaultCountryCode string) string {
	normalized := normalizeNumber(phone, defaultCountryCode)
	return strings.TrimPrefix(normalized, "+")
}

func getDBPath() string {
	exePath, err := os.Executable()
	if err != nil {
		return "whatsapp_session.db"
	}
	return filepath.Join(filepath.Dir(exePath), "whatsapp_session.db")
}

func initStore(ctx context.Context, logger waLog.Logger) (*sqlstore.Container, error) {
	dbPath := getDBPath()
	dbLog := waLog.Stdout("Database", "ERROR", true)
	container, err := sqlstore.New(ctx, "sqlite", fmt.Sprintf("file:%s?_pragma=foreign_keys(1)&_pragma=busy_timeout(10000)", dbPath), dbLog)
	if err != nil {
		return nil, fmt.Errorf("failed to open session store: %w", err)
	}
	return container, nil
}

func getClient(ctx context.Context, container *sqlstore.Container, log waLog.Logger) (*whatsmeow.Client, error) {
	deviceStore, err := container.GetFirstDevice(ctx)
	if err != nil {
		return nil, fmt.Errorf("failed to get device: %w", err)
	}
	client := whatsmeow.NewClient(deviceStore, log)
	return client, nil
}

func handlePairPhone(ctx context.Context, args []string) error {
	pairCmd := flag.NewFlagSet("pair-phone", flag.ExitOnError)
	countryCode := pairCmd.String("country-code", "212", "Default country dial code")
	if err := pairCmd.Parse(args); err != nil {
		return err
	}

	if pairCmd.NArg() < 1 {
		return fmt.Errorf("missing phone number\nUsage: whatsapp-caller pair-phone <your_phone_number>")
	}

	rawPhone := pairCmd.Arg(0)
	phoneDigits := normalizeNumberDigitsOnly(rawPhone, *countryCode)

	log := waLog.Stdout("PairPhone", "INFO", true)
	container, err := initStore(ctx, log)
	if err != nil {
		return err
	}

	client, err := getClient(ctx, container, log)
	if err != nil {
		return err
	}

	if client.Store.ID != nil {
		fmt.Println("[*] Device already paired and logged in as:", client.Store.ID.String())
		return nil
	}

	pairedChan := make(chan struct{})
	client.AddEventHandler(func(rawEvt interface{}) {
		switch rawEvt.(type) {
		case *events.PairSuccess:
			fmt.Println("\n[+] Successfully paired and authenticated with phone number!")
			close(pairedChan)
		}
	})

	err = client.Connect()
	if err != nil {
		return fmt.Errorf("failed to connect: %w", err)
	}
	defer client.Disconnect()

	time.Sleep(1500 * time.Millisecond)

	code, err := client.PairPhone(ctx, phoneDigits, true, whatsmeow.PairClientChrome, "Chrome (macOS)")
	if err != nil {
		return fmt.Errorf("failed to request pairing code: %w", err)
	}

	fmt.Println("\n=======================================================")
	fmt.Printf("   YOUR PAIRING CODE:   %s\n", code)
	fmt.Println("=======================================================")
	fmt.Println("👉 Follow these steps on your iPhone/Android:")
	fmt.Println("   1. Open WhatsApp on your phone.")
	fmt.Println("   2. Go to Settings > Linked Devices.")
	fmt.Println("   3. Tap 'Link a Device'.")
	fmt.Println("   4. At the bottom, tap 'Link with phone number instead'.")
	fmt.Printf("   5. Enter this 8-character code: %s\n", code)
	fmt.Println("=======================================================\n")
	fmt.Println("[*] Waiting for authorization from your phone (code expires in ~2 min)...")

	select {
	case <-pairedChan:
		time.Sleep(2 * time.Second)
		fmt.Println("[+] Pairing complete! Session saved to database.")
		return nil
	case <-time.After(150 * time.Second):
		return fmt.Errorf("pairing timed out. Please run the command again")
	}
}

func handleLogin(ctx context.Context) error {
	log := waLog.Stdout("Login", "INFO", true)
	container, err := initStore(ctx, log)
	if err != nil {
		return err
	}

	client, err := getClient(ctx, container, log)
	if err != nil {
		return err
	}

	if client.Store.ID != nil {
		fmt.Println("[*] Device already paired and logged in as:", client.Store.ID.String())
		fmt.Println("[+] Ready to make calls without browser!")
		return nil
	}

	qrChan, _ := client.GetQRChannel(ctx)
	err = client.Connect()
	if err != nil {
		return fmt.Errorf("failed to connect: %w", err)
	}

	fmt.Println("\n=======================================================")
	fmt.Println("  Scan the QR code below with WhatsApp (Linked Devices)")
	fmt.Println("=======================================================")
	fmt.Println("⚠️  IMPORTANT: Do NOT scan with the iPhone Camera app!")
	fmt.Println("   1. Open WhatsApp on your iPhone.")
	fmt.Println("   2. Go to Settings > Linked Devices.")
	fmt.Println("   3. Tap 'Link a Device' (this opens WhatsApp's scanner).")
	fmt.Println("   4. Scan the QR code below:")
	fmt.Println("=======================================================\n")

	for evt := range qrChan {
		if evt.Event == "code" {
			qrterminal.GenerateHalfBlock(evt.Code, qrterminal.L, os.Stdout)
			fmt.Println("\n[*] Waiting for WhatsApp in-app scan (QR refreshes automatically)...")
		} else if evt.Event == "success" {
			fmt.Println("\n[+] Successfully paired and authenticated!")
			break
		} else if evt.Event == "timeout" {
			fmt.Println("\n[-] QR code timed out. Please try again.")
			return fmt.Errorf("QR code timed out")
		}
	}

	time.Sleep(2 * time.Second)
	client.Disconnect()
	return nil
}

func handleStatus(ctx context.Context) error {
	log := waLog.Stdout("Status", "ERROR", true)
	container, err := initStore(ctx, log)
	if err != nil {
		return err
	}
	client, err := getClient(ctx, container, log)
	if err != nil {
		return err
	}
	if client.Store.ID == nil {
		fmt.Println("status: not_logged_in")
		return nil
	}
	fmt.Printf("status: logged_in\njid: %s\npush_name: %s\n", client.Store.ID.String(), client.Store.PushName)
	return nil
}

func handleLogout(ctx context.Context) error {
	dbPath := getDBPath()
	if err := os.Remove(dbPath); err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("failed to remove session database: %w", err)
	}
	fmt.Println("[+] Successfully logged out and cleared session database.")
	return nil
}

func handleSendMessage(ctx context.Context, args []string) error {
	sendCmd := flag.NewFlagSet("send-message", flag.ExitOnError)
	countryCode := sendCmd.String("country-code", "212", "Default country dial code")
	if err := sendCmd.Parse(args); err != nil {
		return err
	}

	if sendCmd.NArg() < 2 {
		return fmt.Errorf("missing target phone number or message text\nUsage: whatsapp-caller send-message <number> <message_text>")
	}

	rawNumber := sendCmd.Arg(0)
	msgText := strings.Join(sendCmd.Args()[1:], " ")
	phoneDigits := normalizeNumberDigitsOnly(rawNumber, *countryCode)
	targetJID := types.NewJID(phoneDigits, types.DefaultUserServer)

	log := waLog.Stdout("Sender", "WARN", true)
	container, err := initStore(ctx, log)
	if err != nil {
		return err
	}

	client, err := getClient(ctx, container, log)
	if err != nil {
		return err
	}

	if client.Store.ID == nil {
		return fmt.Errorf("not logged in. Run './whatsapp-caller pair-phone <number>' first to link your device")
	}

	fmt.Println("[*] Connecting to WhatsApp Multi-Device network...")
	if err := client.Connect(); err != nil {
		return fmt.Errorf("failed to connect to WhatsApp: %w", err)
	}
	defer client.Disconnect()

	time.Sleep(1 * time.Second)

	fmt.Printf("[*] Sending message to %s...\n", targetJID.String())

	resp, err := client.SendMessage(ctx, targetJID, &waProto.Message{
		Conversation: proto.String(msgText),
	})
	if err != nil {
		return fmt.Errorf("failed to send message: %w", err)
	}

	fmt.Printf("[+] Message sent successfully! Timestamp: %v, Server ID: %s\n", resp.Timestamp, resp.ID)
	return nil
}

func handleCall(ctx context.Context, args []string) error {
	callCmd := flag.NewFlagSet("call", flag.ExitOnError)
	video := callCmd.Bool("video", false, "Make a video call instead of a voice call")
	countryCode := callCmd.String("country-code", "212", "Default country dial code")
	playFile := callCmd.String("play", "", "Path to .mp3 or .wav audio file to play during call")
	recordFile := callCmd.String("record", "", "Path to .wav file to record call audio")
	duration := callCmd.Int("duration", 25, "Call duration in seconds before auto-hangup")
	pcmStdio := callCmd.Bool("pcm-stdio", false,
		"Live mode: read agent audio as s16le 16 kHz mono PCM on stdin and write the "+
			"peer's audio to stdout in the same format. Status lines go to stderr so "+
			"stdout carries audio only.")

	if err := callCmd.Parse(args); err != nil {
		return err
	}

	if callCmd.NArg() < 1 {
		return fmt.Errorf("missing target phone number\nUsage: whatsapp-caller call <number> [--video] [--play file.mp3] [--duration 30]")
	}

	// In live mode stdout is the audio stream, so status has to leave on stderr.
	// Printed to stdout it was both invisible to the caller and mixed into the
	// audio as noise.
	status := os.Stdout
	if *pcmStdio {
		status = os.Stderr
	}
	say := func(format string, args ...any) { fmt.Fprintf(status, format, args...) }

	rawNumber := callCmd.Arg(0)
	targetPhone := normalizeNumber(rawNumber, *countryCode)

	// The whatsmeow logger writes to stdout, which in live mode is the audio
	// stream. Silencing it there keeps the samples clean; its own status is
	// already reported above on stderr.
	log := waLog.Stdout("Caller", "WARN", true)
	if *pcmStdio {
		log = waLog.Noop
	}
	container, err := initStore(ctx, log)
	if err != nil {
		return err
	}

	client, err := getClient(ctx, container, log)
	if err != nil {
		return err
	}

	if client.Store.ID == nil {
		return fmt.Errorf("not logged in. Run './whatsapp-caller pair-phone <number>' first to link your device")
	}

	// Initialize meowcaller VoIP engine BEFORE connecting whatsmeow client
	// The library is silent unless given a logger. Without it there is no way to
	// tell "no inbound RTP arrived" from "RTP arrived and was not decoded",
	// which is exactly the question when the peer's audio comes out silent.
	var voipOpts []meowcaller.Option
	if os.Getenv("MEOWCALLER_DEBUG") != "" {
		voipOpts = append(voipOpts, meowcaller.WithLogger(
			zerolog.New(zerolog.ConsoleWriter{Out: os.Stderr}).
				Level(zerolog.DebugLevel).With().Timestamp().Logger()))
	}
	voipClient := meowcaller.NewClient(client, voipOpts...)

	say("[*] Connecting to WhatsApp Multi-Device servers..." + "\n")
	if err := client.Connect(); err != nil {
		return fmt.Errorf("failed to connect to WhatsApp: %w", err)
	}
	defer client.Disconnect()

	// Wait briefly for stream readiness
	time.Sleep(1 * time.Second)

	say("[*] Initiating %s call to %s...\n", map[bool]string{false: "voice", true: "video"}[*video], targetPhone)

	callOpts := meowcaller.CallOptions{
		Video: *video,
	}

	call, err := voipClient.CallWithOptions(ctx, targetPhone, callOpts)
	if err != nil {
		return fmt.Errorf("failed to place call: %w", err)
	}

	say("[+] Call placed! Call ID: %s\n", call.ID())
	say("[*] Ringing target device..." + "\n")

	callEnded := make(chan string, 1)
	callAnswered := make(chan struct{}, 1)

	call.OnStateChange(func(state meowcaller.CallPhase) {
		say("[*] Call State: %v\n", state)
	})

	call.OnPeerAccept(func() {
		say("[+] Peer ACCEPTED the call! Audio channel is active." + "\n")
		select {
		case callAnswered <- struct{}{}:
		default:
		}
	})

	call.OnEnd(func(reason string) {
		say("[-] Call ended: %s\n", reason)
		select {
		case callEnded <- reason:
		default:
		}
	})

	// Live bidirectional audio. The peer's voice leaves on stdout and the agent's
	// voice arrives on stdin, both s16le 16 kHz mono, which is the same format the
	// cellular path already uses. Status output moves to stderr so a single byte of
	// logging can never be mistaken for a sample.
	if *pcmStdio {
		go func() {
			select {
			case <-callAnswered:
				fmt.Fprintln(os.Stderr, "[*] Live PCM: reading agent audio from stdin")
				call.Play(meowcaller.PCMStream(os.Stdin))
			case <-callEnded:
				return
			}
		}()
		writer := bufio.NewWriterSize(os.Stdout, meowcaller.FrameSamples*2*4)
		var writeMu sync.Mutex
		call.Receive(meowcaller.SinkFunc(func(frame []float32) {
			writeMu.Lock()
			defer writeMu.Unlock()
			buf := make([]byte, len(frame)*2)
			for i, sample := range frame {
				scaled := sample * 32767.0
				if scaled > 32767 {
					scaled = 32767
				} else if scaled < -32768 {
					scaled = -32768
				}
				binary.LittleEndian.PutUint16(buf[i*2:], uint16(int16(scaled)))
			}
			if _, err := writer.Write(buf); err != nil {
				return
			}
			// Flush every frame: a buffered final frame is audible latency.
			_ = writer.Flush()
		}))
		fmt.Fprintln(os.Stderr, "[*] Live PCM: writing peer audio to stdout")
	}

	// Audio Playback handler
	if *playFile != "" {
		go func() {
			select {
			case <-callAnswered:
				say("[*] Playing audio file %s to peer...\n", *playFile)
				var src meowcaller.AudioSource
				var srcErr error
				if strings.HasSuffix(strings.ToLower(*playFile), ".mp3") {
					src, srcErr = meowcaller.MP3File(*playFile)
				} else {
					src, srcErr = meowcaller.WAVFile(*playFile)
				}
				if srcErr != nil {
					say("[-] Failed to open audio source: %v\n", srcErr)
					return
				}
				call.Play(src)
			case <-callEnded:
				return
			}
		}()
	}

	// Audio Recording handler
	if *recordFile != "" {
		sink, recErr := meowcaller.WAVRecorder(*recordFile)
		if recErr != nil {
			say("[-] Failed to initialize audio recorder: %v\n", recErr)
		} else {
			call.Receive(sink)
			say("[*] Recording call audio to %s...\n", *recordFile)
		}
	}

	// Handle Graceful Termination (Ctrl+C or Timeout)
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, os.Interrupt, syscall.SIGTERM)

	timer := time.NewTimer(time.Duration(*duration) * time.Second)
	defer timer.Stop()

	select {
	case reason := <-callEnded:
		say("[+] Call finished (Reason: %s)\n", reason)
	case <-timer.C:
		say("[*] Reached duration limit (%ds). Hanging up...\n", *duration)
		_ = call.Hangup()
	case <-sigChan:
		say("\n[*] Interrupted by user. Hanging up call..." + "\n")
		_ = call.Hangup()
	}

	time.Sleep(1 * time.Second)
	return nil
}

func main() {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	if len(os.Args) < 2 {
		fmt.Printf("WhatsApp Native Standalone Caller & Messaging Engine" + "\n")
		fmt.Printf("\nUsage:" + "\n")
		fmt.Printf("  whatsapp-caller pair-phone <phone_number>   Pair via 8-character pairing code (Easiest)" + "\n")
		fmt.Printf("  whatsapp-caller login                       Pair device via terminal QR code" + "\n")
		fmt.Printf("  whatsapp-caller status                      Check login session status" + "\n")
		fmt.Printf("  whatsapp-caller send-message <number> <msg> Send a text message to any number" + "\n")
		fmt.Printf("  whatsapp-caller call <number> [options]     Initiate native WhatsApp call" + "\n")
		fmt.Printf("  whatsapp-caller logout                      Clear session database" + "\n")
		fmt.Printf("\nCall Options:" + "\n")
		fmt.Printf("  --video              Make a video call instead of voice call" + "\n")
		fmt.Printf("  --country-code       Default country code prefix (default: 212)" + "\n")
		fmt.Printf("  --play <file>        Play .mp3 or .wav audio file into the call" + "\n")
		fmt.Printf("  --record <file.wav>  Record incoming call audio to .wav" + "\n")
		fmt.Printf("  --duration <sec>     Maximum duration in seconds before auto-hangup (default: 25)" + "\n")
		os.Exit(1)
	}

	cmd := os.Args[1]
	switch cmd {
	case "pair-phone":
		if err := handlePairPhone(ctx, os.Args[2:]); err != nil {
			fmt.Fprintf(os.Stderr, "Error: %v\n", err)
			os.Exit(1)
		}
	case "login":
		if err := handleLogin(ctx); err != nil {
			fmt.Fprintf(os.Stderr, "Error: %v\n", err)
			os.Exit(1)
		}
	case "status":
		if err := handleStatus(ctx); err != nil {
			fmt.Fprintf(os.Stderr, "Error: %v\n", err)
			os.Exit(1)
		}
	case "logout":
		if err := handleLogout(ctx); err != nil {
			fmt.Fprintf(os.Stderr, "Error: %v\n", err)
			os.Exit(1)
		}
	case "send-message":
		if err := handleSendMessage(ctx, os.Args[2:]); err != nil {
			fmt.Fprintf(os.Stderr, "Error: %v\n", err)
			os.Exit(1)
		}
	case "call":
		if err := handleCall(ctx, os.Args[2:]); err != nil {
			fmt.Fprintf(os.Stderr, "Error: %v\n", err)
			os.Exit(1)
		}
	default:
		fmt.Fprintf(os.Stderr, "Unknown command: %s\n", cmd)
		os.Exit(1)
	}
}
