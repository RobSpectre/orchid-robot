#!/bin/bash
# Opens a Hermes chat with the Orchid jam skill loaded. The desktop's Jam with Hermes icon runs this.
printf '\e]0;Jam with Hermes\a'
clear
cat <<'TEXT'

   🪻  Jam with Hermes

   Start Orchid first, and play one key and one chord button in orchid-robot
   so both arms are holding. Then ask Hermes for music, for example:

       Play a 20-minute chill set
       Play something dark and slow
       Play a dnb set at 170 bpm

   Hermes asks what you think of each part: answer in your own words and it learns.
   Type  stop  to end the set: the arms go to rest and the music fades out.

TEXT
cd "$HOME/workspace" || exit 1
exec "$HOME/.local/bin/hermes" --skills orchid-robot-jam
