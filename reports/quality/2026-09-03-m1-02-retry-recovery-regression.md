# M1-02 live retry regression and recovery hardening

The authorized 2026-09-03 retry call proved v2 phone delivery (2,943 input frames, 1,498
output frames, 1,503 playout ACKs, zero drops, sequence gaps, underruns, starvation,
concealment, or flush failures). It also exposed two defects after media delivery: phh-su
reported `killall audioserver` exit 1 during an audioserver restart race, and PhoneLLM repeated
a blocked repair after its one regeneration, leaving the caller silent.

Android recovery is now one command-scoped, PID-aware operation. It tolerates an already-gone
audioserver, requires a different live PID within five seconds, captures bounded command output,
exposes recovery detail in health, and clears only the matching stale recovery error. The
installer removes all prior policies for the app UID before inserting and verifying the exact
new command.

Response policy keeps the LLM as the normal conversational authority and performs one observable
persona repair only after the model retry is exhausted. It reserves a response ID, emits Pipecat
response frames, and records an assistant transcript with `response_kind=recovery_fallback`, so
this exceptional path cannot silently drop the caller-facing turn.

Verification: 68 conversation-repair tests pass; Android lifecycle tests pass; Ruff passes for
all changed Python files; APK build and Android protocol gates pass, including
`remote-link-v2-isolation-ok`.

Candidate APK source digest: `01427d6f2ef58fce1e4e0b453ce2fce0c1dd04c77dfeb4a67a17106e651f1831`.
Candidate APK SHA-256: `8c57f02cdb6b4ae9f0368430c94feceb09cf0cc2ceec633fda8beea30f5f7170`.

Hardware installation, reboot persistence, matching backend deployment, and a subsequent
authorized GSM call remain pending because all known Mac SSH routes are currently unreachable.
