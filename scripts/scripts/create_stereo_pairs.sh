#!/bin/sh
# create_stereo_pairs.sh
# Run inside the hassio_audio container to create PulseAudio stereo pair remap sinks.
# Usage: docker exec hassio_audio sh -c "$(cat create_stereo_pairs.sh)"
#
# This enables the Music Assistant "Pulse Audio Out" provider to see individual
# stereo outputs from multi-channel sound cards.

echo "create_stereo_pairs.sh started"

# Remove existing remap sinks to start clean
for id in $(pactl list short modules | awk '/module-remap-sink/ {print $1}'); do
    echo "Unloading existing remap module $id"
    pactl unload-module "$id"
done

pactl unload-module module-suspend-on-idle 2>/dev/null || true

sleep 1

# Define stereo pair channel mappings
# Format: "pair_suffix:left_channel,right_channel"
PAIRS="
front_stereo:front-left,front-right
rear_stereo:rear-left,rear-right
side_stereo:side-left,side-right
center_sub:front-center,lfe
"

# Get full sink list once
sink_list=$(pactl list sinks)

# Extract sink names and their channel maps
echo "$sink_list" | awk '
    /^Sink #/ { sink=""; chmap="" }
    /^\tName:/ { sink=$2 }
    /^\tChannel Map:/ { chmap=$3 }
    sink && chmap { print sink " " chmap; sink=""; chmap="" }
' | while read -r sink chmap; do

    # Skip sendspin and remap sinks
    case "$sink" in
        sendspin_*) continue ;;
    esac

    # Skip sinks with fewer than 4 channels (already stereo)
    chan_count=$(echo "$chmap" | tr ',' '\n' | wc -l)
    if [ "$chan_count" -lt 4 ]; then
        echo "Skipping $sink ($chan_count channels, already stereo)"
        continue
    fi

    # Build a clean card name from the sink name for use in remap sink names
    card=$(echo "$sink" | sed 's/alsa_output\.//' | sed 's/\.[^.]*$//' | tr '.-' '__' | tr -cd '[:alnum:]_')

    echo "Processing $sink (card=$card, channels=$chmap)"
    created=0

    # Try each stereo pair
    echo "$PAIRS" | grep -v '^$' | while IFS=: read -r suffix channels; do
        left=$(echo "$channels" | cut -d',' -f1)
        right=$(echo "$channels" | cut -d',' -f2)

        # Check if this sink has both channels
        if echo "$chmap" | grep -q "$left" && echo "$chmap" | grep -q "$right"; then
            remap_name="${card}_${suffix}"
            echo "  Creating $remap_name ($left, $right)"
            module_id=$(pactl load-module module-remap-sink \
                sink_name="$remap_name" \
                master="$sink" \
                sink_properties="device.description=$remap_name" \
                channels=2 \
                master_channel_map="${left},${right}" \
                channel_map="${left},${right}" \
                remix=no 2>&1)
            if echo "$module_id" | grep -qE '^[0-9]+$'; then
                echo "  Loaded module $module_id for $remap_name"
                created=$((created + 1))
            else
                echo "  Failed to create $remap_name: $module_id"
            fi
        fi
    done
done

echo ""
echo "Done. Current sinks:"
pactl list sinks short
