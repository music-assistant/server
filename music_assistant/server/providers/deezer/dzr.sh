SNG_ID="$1"
DZR_CBC="$2"
./dzr-url $SNG_ID | while read url id; do curl -s "$url" | ./dzr-dec $id; done