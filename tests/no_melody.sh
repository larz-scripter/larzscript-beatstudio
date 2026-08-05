#!/bin/sh
F="$1"
D="$(dirname "$F")"
$BEATSTUDIO init --budget=20 --bpm=120 --file="$F"
$BEATSTUDIO generate larz --bars=2 --seed=3 --no-melody --file="$F"
$BEATSTUDIO beat-render --file="$F"
$BEATSTUDIO melody-render --file="$F"
$BEATSTUDIO report --file="$F"
