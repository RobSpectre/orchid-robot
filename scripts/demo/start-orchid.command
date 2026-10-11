#!/bin/bash
# Start the whole Orchid installation: Orchid Studio, orchid-robot (hardware), the
# Orchid Signal visualizer, then open their pages. The desktop's Start Orchid icon runs this
# in a Terminal window; closing that window (or Ctrl-C) stops what it started.
W="$HOME/workspace"
STUDIO="$W/orchid-studio"
ROBOT="$W/orchid-robot"
VIZ="$W/orchid-visualization"
ROBOT_PY="$HOME/.virtualenvs/orchid/bin/python"
LOGS="$HOME/Library/Logs/Orchid"
BROWSER_PROFILE="$HOME/Library/Application Support/Orchid Browser"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

STUDIO_PORT=8765
ROBOT_PORT=8081
VIZ_PORT=4747

bold=$'\e[1m'; green=$'\e[32m'; yellow=$'\e[33m'; red=$'\e[31m'; dim=$'\e[2m'; off=$'\e[0m'
step() { printf '\n%s▸ %s%s\n' "$bold" "$1" "$off"; }
ok()   { printf '  %s✓ %s%s\n' "$green" "$1" "$off"; }
warn() { printf '  %s! %s%s\n' "$yellow" "$1" "$off"; }
bad()  { printf '  %s✗ %s%s\n' "$red" "$1" "$off"; }

mkdir -p "$LOGS"
printf '\e]0;Orchid — keep this window open\a'
clear
cat <<EOF
${bold}
   🪻  Starting Orchid
${off}
   This window runs the installation. Leave it open while the demo runs.
   Closing it stops everything.
EOF

port_up() { nc -z 127.0.0.1 "$1" >/dev/null 2>&1; }

# Services this window started, as "name|pid|log" entries, so it stops only its own.
STARTED=()

stop_tree() {
    local pid=$1 child
    for child in $(pgrep -P "$pid"); do stop_tree "$child"; done
    kill -TERM "$pid" 2>/dev/null
}

cleanup() {
    trap - EXIT INT TERM HUP
    if ((${#STARTED[@]})); then
        step "Stopping Orchid"
        local entry name pid
        for entry in ${STARTED[@]+"${STARTED[@]}"}; do
            IFS='|' read -r name pid _ <<<"$entry"
            stop_tree "$pid"
            ok "stopped $name"
        done
        warn "The arms keep holding their last position until their power is switched off."
    fi
    exit 0
}
trap cleanup EXIT INT TERM HUP

ask_retry() {  # $1: what to fix. Returns 0 to check again, 1 to go on without it.
    printf '\n  %s%s%s\n' "$yellow" "$1" "$off"
    read -r -p "  Press Enter to check again, or type s and Enter to skip: " answer
    [[ "$answer" != [sS]* ]]
}

# start NAME PORT SECONDS DIR COMMAND... — starts COMMAND in DIR unless PORT already answers.
start() {
    local name=$1 port=$2 limit=$3 dir=$4; shift 4
    local log="$LOGS/$(tr ' ' '-' <<<"$name" | tr '[:upper:]' '[:lower:]').log"
    if port_up "$port"; then
        ok "$name is already running"
        return 0
    fi
    printf '\n=== %s started %s ===\n' "$name" "$(date)" >>"$log"
    (cd "$dir" && exec "$@") >>"$log" 2>&1 </dev/null &
    local pid=$! waited=0
    STARTED+=("$name|$pid|$log")
    printf '  %s… starting %s%s' "$dim" "$name" "$off"
    while ! port_up "$port"; do
        if ! kill -0 "$pid" 2>/dev/null || ((waited >= limit)); then
            printf '\r'; bad "$name did not start. Last lines of $log:"
            tail -n 15 "$log" | sed 's/^/      /'
            return 1
        fi
        sleep 1; ((waited++)); printf '.'
    done
    printf '\r\e[K'; ok "$name is running"
    if [[ "$name" == "orchid-robot" ]]; then caffeinate -i -w "$pid" & fi
    return 0
}

# Keep the Mac and its screen awake while this window is open.
caffeinate -dims -w $$ &

orchid_connected() {
    "$STUDIO/.venv/bin/python" - "$STUDIO/src" <<'PY' 2>/dev/null </dev/null
import sys; sys.path.insert(0, sys.argv[1])
from orchid_studio import midi
port = midi.backend().MidiIn(); found = 'Orchid' in port.get_ports(); port.delete()
sys.exit(0 if found else 1)
PY
}
arms_plugged_in() { compgen -G "/dev/tty.usbmodem*" >/dev/null || compgen -G "/dev/tty.wchusbserial*" >/dev/null; }

step "Checking the hardware"
if ! port_up "$STUDIO_PORT"; then  # a running Studio has already found the Orchid
    until orchid_connected; do
        ask_retry "The Orchid isn't connected. Plug in its USB cable and switch it on." && continue
        warn "Going on without the Orchid: Studio will not hear its keys."; break
    done
    orchid_connected && ok "Orchid is connected"
fi
until arms_plugged_in; do
    ask_retry "No robot arm is plugged in. Connect the arms' USB cables and switch on their power." && continue
    warn "Going on without the arms."; break
done
arms_plugged_in && ok "Robot arm USB found"

# Sound goes to the PA on the headphone jack (see route-audio-to-pa.swift).
ROUTE_SRC="$ROBOT/scripts/demo/route-audio-to-pa.swift"
ROUTE="$HOME/Library/Caches/Orchid/route-audio-to-pa"
if [[ ! -x "$ROUTE" || "$ROUTE_SRC" -nt "$ROUTE" ]]; then
    mkdir -p "$(dirname "$ROUTE")"
    swiftc -O "$ROUTE_SRC" -o "$ROUTE" >>"$LOGS/route-audio.log" 2>&1
fi
while true; do
    said=$("$ROUTE" 2>&1); status=$?
    if ((status == 0)); then
        ok "$said"
        warn "The volume keys don't work on this output: set the level on the PA."
        break
    elif ((status == 2)); then
        ask_retry "The PA isn't plugged in. Connect its cable to the laptop's headphone jack." && continue
    else
        bad "Couldn't send sound to the PA: ${said:-see $LOGS/route-audio.log}"
    fi
    warn "Sound stays on this Mac's current output."; break
done

start_robot() {
    start "orchid-robot" "$ROBOT_PORT" 60 "$ROBOT" \
        "$ROBOT_PY" app.py --enable-hardware --port "$ROBOT_PORT"
}

# Plugging or unplugging a USB-C cable can lose the arms; restarting orchid-robot finds them again.
restart_robot() {
    local entry name pid log robot_pid="" kept=() waited=0
    for entry in ${STARTED[@]+"${STARTED[@]}"}; do
        IFS='|' read -r name pid log <<<"$entry"
        if [[ "$name" == "orchid-robot" ]]; then robot_pid=$pid; else kept+=("$entry"); fi
    done
    step "Restarting orchid-robot"
    if [[ -z "$robot_pid" ]] && port_up "$ROBOT_PORT"; then
        bad "This window didn't start orchid-robot, so it can't restart it."
        warn "Close every Start Orchid window, then double-click Start Orchid again."
        return
    fi
    STARTED=(${kept[@]+"${kept[@]}"})
    if [[ -n "$robot_pid" ]]; then
        stop_tree "$robot_pid"
        while port_up "$ROBOT_PORT" && ((waited < 15)); do sleep 1; ((waited++)); done
        port_up "$ROBOT_PORT" && kill -KILL "$robot_pid" 2>/dev/null && sleep 1
    fi
    start_robot || return
    printf '\n   In the orchid-robot window, press %s⌘R%s to reload it, then %sFind and connect all arms%s.\n\n' \
        "$bold" "$off" "$bold" "$off"
}

step "Starting the programs"
start "Orchid Studio" "$STUDIO_PORT" 120 "$STUDIO" \
    "$STUDIO/scripts/start-macos.sh" --sound-input Orchid --chord-channel 3
start_robot
start "Visualizer" "$VIZ_PORT" 30 "$VIZ" node server/index.mjs


step "Opening the windows"
if pgrep -f "user-data-dir=$BROWSER_PROFILE" >/dev/null; then
    ok "Orchid's browser windows are already open"
else
    # Its own Chrome profile, with background throttling off: a throttled orchid-robot tab
    # misses heartbeats, and after five seconds the arms stop and hold.
    open_window() {
        open -na "Google Chrome" --args --user-data-dir="$BROWSER_PROFILE" \
            --no-first-run --no-default-browser-check \
            --disable-background-timer-throttling --disable-backgrounding-occluded-windows \
            --disable-renderer-backgrounding --app="$1"
        sleep 2
    }
    open_window "http://127.0.0.1:$VIZ_PORT/"
    open_window "http://127.0.0.1:$STUDIO_PORT/"
    open_window "http://127.0.0.1:$ROBOT_PORT/"
    ok "Opened the visualizer, Orchid Studio and orchid-robot"
fi

cat <<EOF

${green}${bold}   Orchid is running.${off}

   ${bold}1.${off} In the ${bold}orchid-robot${off} window, press ${bold}Find and connect all arms${off}.
      Keep that window open. If it is closed, the arms stop and hold where they are.
   ${bold}2.${off} Drag the ${bold}visualizer${off} window to the big screen and press ${bold}F${off} for full screen.
   ${bold}3.${off} To start a jam, double-click ${bold}Jam with Hermes${off} on the desktop.
   ${bold}4.${off} Lost the arms after plugging or unplugging a USB cable? Press ${bold}R${off} in this window
      to restart orchid-robot, then connect again. This happens from time to time.
   ${bold}5.${off} When you're done: in orchid-robot, send the arms to rest, then
      ${bold}Release or disconnect → Disconnect${off} with a hand on the arm. Then close this window.

   ${dim}Pages: orchid-robot http://127.0.0.1:$ROBOT_PORT · Studio http://127.0.0.1:$STUDIO_PORT · visualizer http://127.0.0.1:$VIZ_PORT
   Logs:  $LOGS${off}

EOF

# Watch what this window started, and say so if one of them stops.
reported=" "
while true; do
    for entry in ${STARTED[@]+"${STARTED[@]}"}; do
        IFS='|' read -r name pid log <<<"$entry"
        if [[ "$reported" != *" $pid "* ]] && ! kill -0 "$pid" 2>/dev/null; then
            reported+="$pid "
            bad "$name stopped. Last lines of its log:"
            tail -n 15 "$log" | sed 's/^/      /'
            if [[ "$name" == "orchid-robot" ]]; then
                warn "Press R in this window to restart it."
            else
                warn "Close this window and double-click Start Orchid again to restart everything."
            fi
        fi
    done
    # Wait five seconds for a key: R restarts orchid-robot.
    if read -r -s -n 1 -t 5 key; then
        [[ "$key" == [rR] ]] && restart_robot
    elif (($? == 1)); then
        sleep 5  # no keyboard to read from
    fi
done
