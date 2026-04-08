#!/bin/sh
# create_stereo_pairs.sh
#
# Creates PulseAudio stereo pair remap sinks from multi-channel sound cards.
# Run inside the hassio_audio container to enable the Music Assistant
# "Pulse Audio Out" provider to see individual stereo outputs.
#
# Usage (from the HA host):
#   docker exec hassio_audio sh -c "$(cat create_stereo_pairs.sh)"
#
# Or copy and run directly:
#   docker cp create_stereo_pairs.sh hassio_audio:/tmp/
#   docker exec hassio_audio sh /tmp/create_stereo_pairs.sh
#
# Note: Remap sinks do not survive a PulseAudio restart. Re-run this script
# after hassio_audio restarts, or add a HA automation to run it on startup.

echo "create_stereo_pairs.sh started"

# Remove existing remap sinks to start clean
for id in $(pactl list short modules | awk '/module-remap-sink/ {print $1}'); do
    echo "Unloading existing remap module $id"
    pactl unload-module "$id"
done

# Disable suspend-on-idle to prevent audio dropouts when MA starts playing
pactl unload-module module-suspend-on-idle 2>/dev/null || true

sleep 1

# Stereo pair definitions: suffix:left_channel,right_channel
PAIRS="
front_stereo:front-left,front-right
rear_stereo:rear-left,rear-right
side_stereo:side-left,side-right
center_sub:front-center,lfe
"

# Get full sink list once
sink_list=$(pactl list sinks)

# Parse sink blocks: extract name, channel map, and alsa.card_name
# Output format: sink_name|channel_map|card_name
echo "$sink_list" | awk '
    /^Sink #/ {
        if (sink && chmap) print sink "|" chmap "|" card
        sink=""; chmap=""; card=""
    }
    /^\tName:/ { sink=$2 }
    /^\tChannel Map:/ { chmap=$3 }
    /alsa\.card_name/ {
        match($0, /"[^"]+"/)
        card=substr($0, RSTART+1, RLENGTH-2)
        gsub(/[ \t-]/, "_", card)
        gsub(/[^[:alnum:]_]/, "", card)
    }
    END { if (sink && chmap) print sink "|" chmap "|" card }
' | while IFS='|' read -r sink chmap card; do

    # Skip sendspin virtual sinks
    case "$sink" in
        sendspin_*) continue ;;
    esac

    # Skip sinks with fewer than 4 channels — already stereo, no pairs needed
    chan_count=$(echo "$chmap" | tr ',' '\n' | wc -l)
    if [ "$chan_count" -lt 4 ]; then
        echo "Skipping $sink ($chan_count channels, already stereo)"
        continue
    fi

    # Fall back to sanitized sink name if no alsa.card_name found
    if [ -z "$card" ]; then
        card=$(echo "$sink" \
            | sed 's/^alsa_output\.//' \
            | sed 's/\.[^.]*$//' \
            | tr '.-' '__' \
            | tr -cd '[:alnum:]_')
    fi

    echo "Processing $sink (card=$card, channels=$chmap)"

    # Try each stereo pair definition
    echo "$PAIRS" | grep -v '^$' | while IFS=: read -r suffix channels; do
        left=$(echo "$channels" | cut -d',' -f1)
        right=$(echo "$channels" | cut -d',' -f2)

        # Only create the pair if both channels exist on this sink
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
            else
                echo "  Failed to create $remap_name: $module_id"
            fi
        fi
    done

done

echo ""
echo "Done. Current sinks:"
pactl list sinks short
