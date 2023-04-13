#!/bin/sh
# USAGE Example:
# ./dzr-url 355777961 650744592 | while read url id; do curl -s "$url" | ./dzr-dec $id | mpv - ; done

SNG_IDS=$(printf "%s" "$*" | sed 's/ /,/g')
FETCH=${FETCH:-curl -s} # FETCH="wget -q -O -" or FETCH="curl -s -k"
gw () {
  method="$1"; session="$2" ;apiToken="$3" ; shift 3 # curl args ...
  $FETCH "https://www.deezer.com/ajax/gw-light.php?method=$method&input=3&api_version=1.0&api_token=$apiToken" --header "Cookie: sid=$session" "$@"
}

[ -z "$SNG_IDS" ] && echo "USAGE: dzr-url 5404528,664107" && exit 1
DZR_URL="www.deezer.com/ajax/gw-light.php?method=deezer.ping&api_version=1.0&api_token"
DZR_SID=$($FETCH "$DZR_URL" | jq -r .results.SESSION)
USR_NFO=$(gw deezer.getUserData "$DZR_SID" "$API_TOK")
USR_TOK=$(printf "%s" "$USR_NFO" | jq -r .results.USER_TOKEN)
USR_LIC=$(printf "%s" "$USR_NFO" | jq -r .results.USER.OPTIONS.license_token)
API_TOK=$(printf "%s" "$USR_NFO" | jq -r .results.checkForm)
#printf "SID=$DZR_SID\nAPI=$API_TOK\nLIC=$USR_LIC\nTOK=$USR_TOK\nIDS=$SNG_IDS\n" 1>&2

SNG_NFO=$(gw song.getListData "$DZR_SID" "$API_TOK" --data "{\"sng_ids\":[$SNG_IDS]}")
SNG_TOK=$(printf "%s" "$SNG_NFO" | jq [.results.data[].TRACK_TOKEN])
# reset to resolvable id, eg:no personal uploads (geoblocked track will fail later)
SNG_IDS=$(printf "%s" "$SNG_NFO" | jq [.results.data[].SNG_ID])
URL_NFO=$($FETCH 'https://media.deezer.com/v1/get_url' --data "{\"license_token\":\"$USR_LIC\",\"media\":[{\"type\":\"FULL\",\"formats\":[{\"cipher\":\"BF_CBC_STRIPE\",\"format\":\"MP3_128\"}]}],\"track_tokens\":$SNG_TOK}")
URL_IDS=$(printf "%s" "$URL_NFO" | jq --argjson ids "$SNG_IDS" '[.data , ($ids|map({id:.}))]|transpose|map(add)')
printf "%s" "$URL_IDS" | jq -r '.[]|select(.errors)|[.errors[0].message     ,.id]|@tsv' 1>&2
printf "%s" "$URL_IDS" | jq -r '.[]|select(.media )|[.media[].sources[0].url,.id]|@tsv'
