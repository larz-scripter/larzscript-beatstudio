#!/bin/sh
F="$1"
$BEATSTUDIO init --budget=1 --bpm=120 --file="$F" >/dev/null
$BEATSTUDIO step kick 0 on --file="$F" >/dev/null
$BEATSTUDIO beat-render --file="$F" >/dev/null
$BEATSTUDIO mix beat --gain=0 --file="$F" >/dev/null
$BEATSTUDIO master --price=1000 --file="$F"
