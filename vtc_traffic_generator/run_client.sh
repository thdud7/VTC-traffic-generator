#!/usr/bin/env bash
set -euo pipefail

# manually set the DISPLAY variable for searchlight user
export DISPLAY=:0

python /home/searchlight/vtc/vtc_traffic_generator/vtc_generator.py $1 

