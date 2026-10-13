#!/bin/bash
# Opens a Hermes chat with the Orchid jam skill loaded. The desktop's Jam with Hermes icon runs this.
printf '\e]0;Jam with Hermes\a'
clear
cat <<'TEXT'

   🪻  Jam with Hermes

   Start Orchid first, and play one key and one chord button in orchid-robot
   so both arms are holding. Then ask Hermes for music, for example:

       Pump up the jam: play a 20-minute house set
       Play a chill lo-fi set for 30 minutes
       Play drum and bass at 170 bpm

   Hermes asks what you think of each part: answer in your own words and it learns.
   Type  stop playing  to end the set: Hermes parks the arms and fades the music out.
   Always do that before tearing down for the night. More prompts: Read Me First.

TEXT
cd "$HOME/workspace" || exit 1
exec "$HOME/.local/bin/hermes" --skills orchid-robot-jam
