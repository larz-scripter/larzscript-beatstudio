#!/bin/sh
F="$1"
$BEATSTUDIO init --budget=20 --bpm=120 --file="$F" >/dev/null
$BEATSTUDIO step kick 0 on --file="$F" >/dev/null
$BEATSTUDIO beat-render --file="$F" >/dev/null
$BEATSTUDIO mix beat --gain=0 --file="$F" >/dev/null
$BEATSTUDIO remaster --file="$F"
$BEATSTUDIO master --price=2 --low=3 --file="$F"
$BEATSTUDIO remaster --low=-3 --high=4 --file="$F"
$BEATSTUDIO report --file="$F" | tail -1
