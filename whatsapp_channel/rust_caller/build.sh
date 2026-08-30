#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"

cargo build --manifest-path "$DIR/Cargo.toml" --release
cp "$DIR/target/release/phoneagent-whatsapp-rust-caller" \
   "$ROOT/whatsapp-rust-caller"
chmod 0755 "$ROOT/whatsapp-rust-caller"

echo "Built $ROOT/whatsapp-rust-caller"

