#!/bin/sh
# NAME
# 	dzr-dec - decode a deezer track by it ID
# USAGE:
# 	dzr-dec 321654 < enc.mp3 > dec.mp3

SNG_ID="$1"
[ -z "$DZR_CBC" ] && echo "Missing 'DZR_CBC' env variable" 1>&2 && exit 1
[ -z "$SNG_ID"  ] && echo "USAGE: DZR_CBC=XXXX dzr-dec 1234 < enc.mp3 > dec.mp3" 1>&2 && exit 1
dzr_cbc_hex=$(printf "$DZR_CBC"                                  | od -An -t x1 |tr -d '\n ')
track_md5_l=$(printf "%s" "$SNG_ID" | openssl md5 -r|cut -b1-16  | od -An -t x1 |tr -d '\n ')
track_md5_r=$(printf "%s" "$SNG_ID" | openssl md5 -r|cut -b17-32 | od -An -t x1 |tr -d '\n ')
track_key=$(for k in 1 3 5 7 9 11 13 15 17 19 21 23 25 27 29 31; do # no seq in BSD
	a=$(printf $dzr_cbc_hex | cut -b $k-$(($k+1)))
	b=$(printf $track_md5_l | cut -b $k-$(($k+1)))
	c=$(printf $track_md5_r | cut -b $k-$(($k+1)))
	printf '%02x' "$((0x$a ^ 0x$b ^ 0x$c))"
done)

stripe_size=2048
openssl_opt="-d -nopad -bufsize $stripe_size -K $track_key -iv 0001020304050607"
# OpenSSL 3 require some extra argument
if openssl bf-cbc -help 2>&1 | grep -q provider; then
	openssl_opt="$openssl_opt -provider legacy";
fi
# And now for my next loop, I'd like to return to the classics
set -e; # if an iteration fail we stop the loop (you better pipe me with curl)
while true; do
    LC_ALL=POSIX dd bs=$stripe_size count=1 status=none | openssl bf-cbc $openssl_opt 2>/dev/null 1>&4
  { LC_ALL=POSIX dd bs=$stripe_size count=2 2>&3 >&4; } 3>&1 | grep -qe '^0[+]0 ' && break
done 4>&1
