# Community Catalog Controller

Installs and loads providers that community members build and maintain in their own repositories. The Music Assistant project curates a registry that publishes an index per server line (`nightly`, `beta`, `<major>.<minor>`). A server installs only what the index of its own line offers, and stops loading whatever the index blocks. The catalog is off until an admin switches it on; nothing of it runs in safe mode.

## Package Layout

- `controller.py`: `CommunityCatalogController`, a `CoreController` (domain `community_catalog`) that owns the switch, the start, the daily index check, the admin commands, install and remove, the moved providers, and the decision state of each installed provider. What is installed or loaded only changes through it, always under one `asyncio.Lock`; the modules below do that work when it calls them.
- `gate.py`: `evaluate()`, the pure function that decides whether an installed provider may load, switches to another version or has an update on offer, given what was installed, the content hash of its live folder and the kept copy of the index; `Gate`, which asks it with the kept copy; and the reason texts of the verdicts.
- `versions.py`: `VersionChanger`, which stages and activates another version of an installed provider (live, or pending until the next start), judges the first load of a version and goes back from one that fails.
- `loader.py`: `ProviderLoader`, which installs the requirements of community providers, registers them, loads their settings entries and stops them; it also reads the development folder.
- `listing.py`: the functions that build the installed and available providers as the app lists them.
- `store.py`: `CatalogStore`, the only writer of `index.json` and `state.json`.
- `index.py`: `IndexClient`, which fetches the index of the server's line (with its ETag) and hands a valid one to the store.
- `installer.py`: `Installer`, which downloads a version from GitHub into a staging folder, checks it against the content hash the index lists, and swaps it live; `content_hash()` is the definition the registry uses too.
- `dependencies.py`: `CommunityDependencies`, which resolves the Python requirements of all community providers in one pass against the server's own packages and installs the result into one folder next to the server.
- `models.py`: the index, the state and the API result models; `constants.py`: setting keys, file format versions and the moved providers.

## On Disk

```
<data>/community_providers/   second location of the music_assistant.providers package
  <domain>/                   live code of an installed provider
  .previous/<domain>/         the version that was live before, to go back to
  .staging/<domain>/          a download being checked, or the version that becomes live at the next start
  index.json                  kept copy of the last valid index
  state.json                  the ETag and, per domain, the installed version, repository, commit and content hash,
                              whether that version loaded once, the previous, pending and failed version and a
                              notice; and the moved providers the server installed by itself
<cache>/community_deps/       the requirements of all community providers, and requirements.lock
```

Nothing is created until the catalog is switched on or a provider is installed. A `state.json` of an unknown format reads as empty: nothing loads and nothing is deleted. A provider folder that is not in the state never loads and is not listed, whoever put it there.

## When a Community Provider Loads

A community provider is registered (its manifest and texts) only while it may load. Both ways the server imports provider code start from a registered manifest, so not registering is the whole gate. Its settings entries are ordinary provider entries; while the provider is not registered they are served as entries without a provider, with the reason from `unavailable_reason()`. An installed provider whose manifest names no support link gets its repository as one, so the app sends questions to its maintainer.

At start (a background task after the server's own providers, never in safe mode):

1. The kept copy is read and, while the switch is on, anything is installed or a moved provider waits, the index is fetched; the daily check is scheduled.
2. Each moved provider that waits is installed (see Moved Providers).
3. A pending version becomes live when its staged folder still has the content hash the index lists for it; otherwise it is dropped and the installed version stays.
4. Every installed provider gets the content hash of its live folder and a verdict from `evaluate()`. A changed folder is installed again at the same version, which keeps the previous version; if that fails, it does not load.
5. Where the verdict is a switch, and with automatic updates on also where an update is on offer, the new version is staged and made live (see Updates). A switch that fails does not load; an update that fails leaves the installed version loading.
6. The requirements of everything that may load, together with those of the providers of the development folder, are resolved in one pass and synced. A provider whose requirements clash is left out and the rest is resolved again.
7. Each remaining provider is registered and its enabled settings entries load; a version that never loaded here before is judged (see Going Back).

`evaluate()` decides in this order: without any kept copy nothing loads; a blocked provider or blocked version does not load, and a blocked version switches to the latest version when the provider is listed and the latest version is not one this server went back from; with only a copy of another line, a provider loads when its folder is unchanged; a provider that is no longer listed does not load; an installed version that is not listed for this line, or listed with other content, switches to the latest version when the provider is listed and the latest version is not one this server went back from, and does not load otherwise; a changed folder is installed again; everything else loads, including a withdrawn provider, with an update on offer when the provider is listed and its latest version is another one that this server did not go back from.

After a fetch that changed the index (the daily check or a refresh), `evaluate()` runs again with the folder hashes taken at start. A loaded provider that may no longer load is stopped: its manifest unregistered, its instances unloaded and its modules forgotten. Its files, state and settings entries stay. A provider that did not load stays so until the next start or until its switch is done. A running version that is no longer listed keeps running until it is switched; it is stopped only when there is nothing to switch to. After every check of the index, changed or not, the switches, and with automatic updates on the updates on offer, are carried out as below, so one that failed before is tried again.

## Install and Remove

Install downloads the latest version the index offers, resolves its requirements together with those of every loaded community provider, installs them, makes the version live, writes the state and registers the provider. A clash, a download that does not match the index, or a requirement change to a package that is already imported refuses the install before anything goes live. No settings entry is created: the provider appears among the manifests and is added the usual way.

Remove drops the provider from the state, stops it and deletes its folders. Its settings entries stay. Its requirements stay in the dependency folder until the next start resolves without them.

## Updates

An update and a switch to the latest version follow one path. The new version is staged, and its requirements are resolved together with those of every loaded community provider; a clash discards the staged version and refuses the change, naming what it clashes with. When the result would replace or remove an installed package that is already imported, the staged version is kept as the pending version and the next start makes it live; the running version keeps running and the list says a restart is needed. Staging another version replaces the pending one; when that fails, no version is pending. Otherwise the change happens live: the requirements are synced, the provider and every loaded community provider that declares `depends_on` it are stopped, the new version is made live (the old one moves to `.previous`), and they are registered and loaded again. Other providers keep running. Every step before the new version is made live leaves the installed version as it was.

The admin can update one provider or all at once, whatever is playing. Switches happen by themselves; updates do so only when the admin turns on automatic updates (the hidden `auto_update` setting, which only applies while the catalog is switched on). Such changes happen at start before anything loads, and after every check of the index or when automatic updates are turned on only while no player is playing; otherwise they are tried again every hour until done.

## Going Back

A version that has not loaded on this server before is judged after it loads, also at start. It is verified as soon as one of its enabled settings entries loads, or, without any enabled entry, when its module imports. When the load fails with an error that is not a `MusicAssistantError` (a bug, rather than a handled condition such as a failed login or an unreachable service), or the module does not import, the server goes back: it stops the provider and its dependents, puts the previous version back, records the failed version, syncs the requirements of the previous version and loads it when `evaluate()` lets it. When those requirements would change an imported package, the provider stays stopped until a restart finishes going back. A first install has nothing to go back to; its settings entry shows the load error as for any provider. A handled error never goes back.

At a start where the requirements could not be installed, a version that fails may only miss them: the server goes back, but does not record the version as failed, so it is offered again. A recorded failed version is not switched to, offered or updated to automatically, and the list shows it. The admin can still install it by updating that one provider; making it live drops the record, which is set again when it fails again.

## Moved Providers

A provider that moves from the server to the community catalog is listed in `MOVED_PROVIDER_DOMAINS`. It waits while it has a settings entry, is not installed, the server has no provider of its own by that name and the server did not install it by itself before. While one waits the index is needed, also with the switch off, and its settings entries carry a reason of their own. At start, before the installed providers are gated, and at every check of the index (the daily check or a refresh), the server installs it the way an admin's install does, without the switch. When the catalog does not offer it or the install fails, a warning is logged, its settings entries say why, and it keeps waiting.

The state records the move, so a provider the admin removes afterwards is never installed by itself again. The installed provider carries the notice `moved` in the list until the admin dismisses it; otherwise it is an ordinary installed community provider.
