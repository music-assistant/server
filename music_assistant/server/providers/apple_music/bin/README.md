# Content Decryption Module (CDM)
You need a custom CDM if you would like to playback Apple music on your local machine. The music provider expects two files to be present in your local user folder `/usr/local/bin/widevine_cdm` :

1. client_id.bin
2. private_key.pem

These two files allow Music Assistant to decrypt Widevine protected songs. More info on how you can obtain your own CDM files can be found [here](https://www.ismailzai.com/blog/picking-the-widevine-locks).
