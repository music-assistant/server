# Remote Radio Catalog

[English documentation](README.md)

Met deze provider abonneer je Music Assistant op een eigen online lijst met
radiostations. Elke toegevoegde provider is één afzonderlijke catalogus. Je kunt
de provider dus meerdere keren toevoegen, met een andere lijst en andere instellingen.
Er worden geen stations of catalogusadressen meegeleverd.

## Status

Dit is een eerste implementatie voor de ontwikkelversie van Music Assistant Server.
Hij is nog niet opgenomen in een officiële Music Assistant-release en verschijnt
daarom niet vanzelf in bestaande installaties. Dit is geen HACS-integratie of
Home Assistant-add-on. Voor ontwikkelaars: gebruik de volledige serverbranch,
inclusief de kleine aanpassing aan de vergelijking van unieke radiostations.
Alleen de providermap kopiëren neemt die bescherming tegen samenvoegen niet mee.

## Instellen

Zodra je een serverversie met deze provider gebruikt:

1. Open de instellingen van Music Assistant en voeg een muziekprovider toe.
2. Kies **Remote Radio Catalog**.
3. Vul het HTTP- of HTTPS-adres van één M3U-, M3U8- of JSON-catalogus in.
4. Kies het verversingsinterval in minuten: minimaal 1, maximaal 1440 (één dag).
5. Geef de provider desgewenst een herkenbare naam via het gewone naamveld van MA.
6. Herhaal deze stappen voor iedere volgende catalogus.

Bijvoorbeeld: “My Icecast Stations” iedere 5 minuten, “Community Radio” iedere
30 minuten en “Personal Radio Collection” ieder uur. Namen, stations en
verversingen blijven per instantie gescheiden.

Het interval wijzig je in de opties van die provider. Het catalogusadres wijzig
je via de ingebouwde herconfiguratie van die provider. MA herlaadt alleen de
gewijzigde instantie. Een nieuwe URL neemt geen oude catalogusgegevens over.

Onder Bladeren heeft iedere catalogus zijn eigen bron. Ook de bronvermelding bij
een station gebruikt de ingestelde providernaam. Verwijderen van één provider
stopt alleen zijn taken en verwijdert alleen zijn eigen cache. Het opruimen van
bibliotheekvermeldingen verloopt via Music Assistant zelf.

## Afspelen en storingen

Afspelen verloopt via de bestaande spelers van Music Assistant. Voor AirPlay of
Chromecast voeg je die spelerondersteuning in MA toe; deze catalogusprovider
introduceert geen eigen spelerprotocol. De feitelijke stream moet bereikbaar en
afspeelbaar zijn. Afspelen op fysieke speakers is nog niet in deze werkomgeving getest.

Bij een onbereikbare of ongeldige catalogus blijft de laatst geldige lijst beschikbaar
voor bladeren en afspelen, ook na een herstart. Een mislukte bibliotheeksynchronisatie
veroorzaakt geen verwerking van een lege lijst. Zonder eerdere geldige lijst wordt een
tijdelijke fout getoond. Andere catalogi blijven normaal werken.

Een **geldig lege** catalogus is toegestaan: `[]`, `{"stations": []}` of alleen
`#EXTM3U`. MA past hierop zijn standaardinstellingen voor bibliotheekverwijderingen toe.
Wanneer bibliotheeksynchronisatie is uitgeschakeld, blijft de catalogus verversen en
beschikbaar onder Bladeren, maar worden stations niet automatisch geïmporteerd.

Gebruik uitsluitend catalogi en streams waarvoor je de benodigde toestemming hebt.
De provider kan niet controleren of een aanbieder over de benodigde rechten beschikt.

## Catalogusformaat voor beheerders

Bestanden moeten UTF-8 zijn en mogen maximaal 2 MiB groot zijn. Alleen HTTP(S)-adressen
zijn toegestaan; in de URL opgenomen gebruikersnamen en wachtwoorden niet.
M3U8 betekent hier een stationlijst in UTF-8, niet een HLS-lijst met audiofragmenten.
Een individuele station-URL mag uiteraard naar een HLS-stream verwijzen.

Voorbeeld M3U/M3U8:

```m3u
#EXTM3U
#EXTINF:-1,Voorbeeldradio
https://radio.example.org/live.mp3
```

Voorbeeld JSON:

```json
{
  "stations": [
    {
      "id": "voorbeeldradio",
      "name": "Voorbeeldradio",
      "url": "https://radio.example.org/live.mp3",
      "logo": "https://radio.example.org/logo.png"
    }
  ]
}
```

Een losse JSON-array is ook toegestaan. `url` is verplicht; `id`, `name` en `logo`
zijn optioneel. Zonder ID wordt een stabiele SHA-256 van de stream-URL gebruikt.
Gebruik bij voorkeur een blijvend ID wanneer een stream-URL kan wijzigen.
Relatieve stream- en logo-adressen worden ten opzichte van het catalogusadres
na eventuele redirects opgelost. Een Icecast-statusdocument met `icestats.source`
wordt ondersteund; daarbij worden `listenurl` en `server_name` gebruikt.

Een dubbel ID met verschillende gegevens binnen **één** lijst maakt die download
ongeldig. Hetzelfde ID in **verschillende** providerinstanties is toegestaan.
Bij ongeldige gegevens wordt de gehele nieuwe lijst afgewezen; er wordt geen
gedeeltelijke lijst opgeslagen die per ongeluk stations zou verwijderen.

## Technische opzet en tests

- Het manifest staat meerdere instanties toe (`multi_instance: true`).
- `is_streaming_provider = False` voorkomt het delen van catalogi tussen instanties.
- Media-items en `ProviderMapping` gebruiken de native `instance_id`; mappings zijn uniek.
- MA's naamvergelijking respecteert unieke radiomappings, zodat gelijke namen en IDs
  niet alsnog via de bibliotheek worden samengevoegd.
- De native `mass.call_later`-timer en downloadtaak krijgen een instance-specifiek ID.
  De algemene MA-taakplanning ondersteunt in deze basisversie uren/dagen, geen minuten.
- Een timer start alleen `music.start_sync(providers=[instance_id])`. Importeren en
  verwijderen blijven in MA's bestaande bibliotheeksynchronisatie.
- Cachegegevens zijn gescheiden op `instance_id` én gecontroleerd met een hash van
  het catalogusadres. Alleen volledig gevalideerde downloads vervangen de laatste goede lijst.
- Gelijktijdige aanvragen binnen één instantie delen één download. Annuleren van
  één aanvraag verstoort andere wachtende aanvragen niet; unload annuleert en wacht
  op de eigen achtergrondtaken.

Gerichte tests staan in `tests/providers/remote_radio_catalog/`. Ze gebruiken echte
HTTP-aanvragen naar een plaatselijke testserver en de echte MA-provider- en vergelijkingscode;
configuratie, cacheopslag en timers worden gecontroleerd nagebootst.

In een volledig ingerichte MA-ontwikkelomgeving:

```sh
pytest tests/providers/remote_radio_catalog tests/helpers/test_compare.py
pre-commit run --all-files
```

Een volledige Linux-servertest met echte database, toevoegen/verwijderen via de UI
en fysieke spelers blijft nodig voordat deze implementatie productierijp heet.
