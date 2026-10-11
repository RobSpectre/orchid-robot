#!/bin/bash
# Make a macOS account for running the Orchid installation, sharing this account's projects.
#
#   sudo scripts/demo/setup-demo-user.sh [account-name]      (default: demo)
#
# It creates the account as an administrator (so it can use sudo), asking you for its
# password; links every project in your ~/workspace into its ~/workspace, along with the
# robot's Python environment, LeRobot's calibration and Hermes' state; lets both accounts
# read and write those files; and puts a Start Orchid icon on its desktop. Run it again at
# any time: it only adds what is missing. Your files' contents are never changed; they only
# gain access entries (ls -le shows them) for the demo account and you.
set -euo pipefail

DEMO=${1:-demo}
OWNER=${SUDO_USER:-}
if [[ $EUID -ne 0 || -z "$OWNER" || "$OWNER" == root ]]; then
    echo "Run this from your own account with sudo: sudo $0 ${1:-}" >&2
    exit 1
fi
OWNER_HOME=$(dscl . -read "/Users/$OWNER" NFSHomeDirectory | awk '{print $2}')
DEMO_HOME="/Users/$DEMO"
HERE=$(cd "$(dirname "$0")" && pwd -P)
SCRIPTS="workspace/orchid-robot/scripts/demo"

echo "==> Account '$DEMO'"
if id "$DEMO" >/dev/null 2>&1; then
    echo "    already exists"
else
    echo "    Choose a password for '$DEMO' when asked:"
    sysadminctl -addUser "$DEMO" -fullName "Orchid Demo" -home "$DEMO_HOME" -password - -admin
    createhomedir -c -u "$DEMO" >/dev/null
fi
# Administrators can use sudo.
dseditgroup -o checkmember -m "$DEMO" admin >/dev/null 2>&1 || dseditgroup -o edit -a "$DEMO" -t user admin
DEMO_HOME=$(dscl . -read "/Users/$DEMO" NFSHomeDirectory | awk '{print $2}')
[[ -d "$DEMO_HOME" ]] || createhomedir -c -u "$DEMO" >/dev/null

# Both accounts get full access to the shared files, and new files inherit it, so whichever
# account writes a database, log or taught key, the other can still change it.
SHARED_PERMS="read,write,append,delete,readattr,writeattr,readextattr,writeextattr,readsecurity,list,search,add_file,add_subdirectory,delete_child,execute,file_inherit,directory_inherit"
share() {
    echo "    $1"
    # find never follows symlinks, so links inside the tree can't widen access elsewhere.
    for who in "$DEMO" "$OWNER"; do
        find "$1" ! -type l -exec chmod +a "user:$who allow $SHARED_PERMS" {} + 2>/dev/null || true
    done
}
traverse() {  # Let the demo account pass through a directory without listing it.
    chmod +a "user:$DEMO allow search" "$1"
}
as_demo() { sudo -H -u "$DEMO" "$@"; }
link() {  # link TARGET LINK, made by the demo account so it owns the link and its folders
    if [[ -e "$2" && ! -L "$2" ]]; then
        echo "    leaving ~$DEMO/${2#"$DEMO_HOME"/} alone: it is a real file or folder"
        return
    fi
    as_demo mkdir -p "$(dirname "$2")"
    as_demo ln -sfn "$1" "$2"
    echo "    ~$DEMO/${2#"$DEMO_HOME"/}"
}

echo "==> Sharing files (this can take a minute)"
traverse "$OWNER_HOME"
share "$OWNER_HOME/workspace"
[[ -d "$OWNER_HOME/.virtualenvs/orchid" ]] && traverse "$OWNER_HOME/.virtualenvs" && share "$OWNER_HOME/.virtualenvs/orchid"
if [[ -d "$OWNER_HOME/.cache/huggingface/lerobot" ]]; then
    traverse "$OWNER_HOME/.cache"; traverse "$OWNER_HOME/.cache/huggingface"
    share "$OWNER_HOME/.cache/huggingface/lerobot"
fi
[[ -d "$OWNER_HOME/.hermes" ]] && share "$OWNER_HOME/.hermes"

echo "==> Links in $DEMO_HOME"
for project in "$OWNER_HOME"/workspace/*; do
    link "$project" "$DEMO_HOME/workspace/$(basename "$project")"
done
for path in .virtualenvs/orchid .cache/huggingface/lerobot .hermes .local/bin/hermes; do
    [[ -e "$OWNER_HOME/$path" ]] && link "$OWNER_HOME/$path" "$DEMO_HOME/$path"
done
# The projects belong to $OWNER; let git work in them from the demo account too.
as_demo git config --global --replace-all safe.directory '*'

# Pistil keeps its settings and activation per account.
PISTIL="Library/Application Support/Pistil.settings"
if [[ -f "$OWNER_HOME/$PISTIL" && ! -e "$DEMO_HOME/$PISTIL" ]]; then
    echo "==> Copying Pistil's settings"
    as_demo mkdir -p "$DEMO_HOME/Library/Application Support"
    install -o "$DEMO" -m 600 "$OWNER_HOME/$PISTIL" "$DEMO_HOME/$PISTIL"
fi

echo "==> Desktop: Start Orchid, Jam with Hermes and the guide"
as_demo mkdir -p "$DEMO_HOME/Desktop"
desktop_app() {  # desktop_app NAME COMMAND-FILE ICON: an app that opens COMMAND-FILE in Terminal
    local app="$DEMO_HOME/Desktop/$1.app"
    rm -rf "$app"
    as_demo osacompile -o "$app" -e "do shell script \"open -a Terminal \" & quoted form of (POSIX path of (path to home folder) & \"$SCRIPTS/$2\")"
    # Use our icon: drop the compiled-in one (it takes precedence), then re-sign the changed app.
    install -o "$DEMO" -m 644 "$HERE/$3" "$app/Contents/Resources/applet.icns"
    rm -f "$app/Contents/Resources/Assets.car"
    as_demo /usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$app/Contents/Info.plist" 2>/dev/null || true
    as_demo codesign --force -s - "$app" 2>/dev/null
    as_demo touch "$app"
}
desktop_app "Start Orchid" start-orchid.command StartOrchid.icns
desktop_app "Jam with Hermes" jam-with-hermes.command JamWithHermes.icns
link "$DEMO_HOME/$SCRIPTS/guide.html" "$DEMO_HOME/Desktop/Read Me First.html"

cat <<EOF

Done. Log in as '$DEMO' (or switch to it from the menu bar) and open Read Me First on the
desktop. It walks through Start Orchid, training the arms and Jam with Hermes.

Plug the PA into the laptop's headphone jack: Start Orchid sends the sound there.

Before switching, quit Orchid Studio, orchid-robot and the visualizer in this account:
only one account can use the arms and the Orchid at a time.
EOF
