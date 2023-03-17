FETCH=${FETCH:-curl -s}
unscramble(){ printf "${8}${16}${7}${15}${6}${14}${5}${13}${4}${12}${3}${11}${2}${10}${1}${9}";}
APP_WEB=$($FETCH -L deezer.com/en/channels/explore | sed -n 's/.*src="\([^"]*app-web[^"]*\).*/\1/p' | xargs $FETCH -L)
TMP_CBC=$(echo "$APP_WEB" | tr ,_ '\n' | sed -n 's/.*\(%5B0x[63]1%.\{48\}%5D\).*/\1/p' | sed 's/%5[BD]//g;s/%/\\x/g' | xargs -0 printf %b | sed 's/0x/\\x/g' | tr , '\n' | xargs -0 printf %b)
DZR_CBC=$(unscramble $TMP_CBC);
echo -e $DZR_CBC
