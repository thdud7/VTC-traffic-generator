import ffmpeg
import pulsectl
import time
import json

# Load config(s)
with open('config.json', 'r') as infile:
    config = json.load(infile)

# Initialize client, call from controller
# Set pulse audio devices and device volume
# Check for v4l2 kernel mod
def initialize_vtc_client():
    print ("Initializing VTC client")
