#!/bin/sh
F="$1"
D="$(dirname "$F")"
$BEATSTUDIO init --budget=20 --bpm=120 --file="$F"
$BEATSTUDIO generate larz --bars=1 --seed=2 --file="$F"
$BEATSTUDIO beat-render --file="$F"
$BEATSTUDIO master --price=1 --target-lufs=-14 --file="$F"
$BEATSTUDIO remaster --target-lufs=-23 --file="$F"
$BEATSTUDIO report --file="$F"
