<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import LightStage from './components/LightStage.vue'
import SongTimeline from './components/SongTimeline.vue'
import { useTransport } from './composables/useTransport'
import { parseFramesFile, type FramesFile, type StageView } from './types'

const data = shallowRef<FramesFile | null>(null)
const fileName = ref('')
const error = ref('')
const audio = shallowRef<HTMLAudioElement | null>(null)
const audioName = ref('')
const offsetMs = ref(0)
const view = ref<StageView>('top')
const showNames = ref(true)
const dragging = ref(false)

const duration = computed(() => data.value?.duration_s ?? 0)
const { time, playing, rate, toggle, pause, seek } = useTransport(duration, audio, offsetMs)

const colors = computed(() => {
  const d = data.value
  if (!d) return null
  return d.frames[Math.min(d.frames.length - 1, Math.floor(time.value * d.fps))] ?? null
})

const position = computed(() => {
  const beats = data.value?.beats ?? []
  let lo = 0
  let hi = beats.length - 1
  let idx = -1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (beats[mid][0] <= time.value) {
      idx = mid
      lo = mid + 1
    } else hi = mid - 1
  }
  if (idx < 0) return 'Before first beat'
  let bar = 0
  let lastDown = 0
  for (let i = 0; i <= idx; i++) {
    if (beats[i][1]) {
      bar++
      lastDown = i
    }
  }
  return `Bar ${Math.max(bar, 1)}, beat ${idx - lastDown + 1}`
})

function clock(s: number) {
  const m = Math.floor(s / 60)
  return `${m}:${(s - m * 60).toFixed(1).padStart(4, '0')}`
}

async function openFrames(file: File) {
  try {
    const parsed = parseFramesFile(await file.text())
    pause()
    data.value = parsed
    fileName.value = file.name
    error.value = ''
    seek(0)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

function openAudio(file: File) {
  if (audio.value) URL.revokeObjectURL(audio.value.src)
  pause()
  audio.value = new Audio(URL.createObjectURL(file))
  audioName.value = file.name
}

function onPick(e: Event, kind: 'frames' | 'audio') {
  const file = (e.target as HTMLInputElement).files?.[0]
  if (!file) return
  if (kind === 'frames') void openFrames(file)
  else openAudio(file)
  ;(e.target as HTMLInputElement).value = ''
}

function onDrop(e: DragEvent) {
  dragging.value = false
  for (const file of Array.from(e.dataTransfer?.files ?? [])) {
    if (file.type.startsWith('audio/')) openAudio(file)
    else void openFrames(file)
  }
}

function onKey(e: KeyboardEvent) {
  if (e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement) return
  if (!data.value) return
  const step = e.shiftKey ? 5 : 1
  if (e.code === 'Space') toggle()
  else if (e.key === 'ArrowLeft') seek(time.value - step)
  else if (e.key === 'ArrowRight') seek(time.value + step)
  else if (e.key === '[') offsetMs.value -= 10
  else if (e.key === ']') offsetMs.value += 10
  else return
  e.preventDefault()
}

onMounted(() => window.addEventListener('keydown', onKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div
    class="shell"
    :class="{ dragging }"
    @dragover.prevent="dragging = true"
    @dragleave.self="dragging = false"
    @drop.prevent="onDrop"
  >
    <header>
      <h1>Hue lab</h1>
      <p v-if="data" class="source">
        <span>{{ fileName }}</span>
        <span class="dim">{{ data.engine }}{{ data.mode ? `, ${data.mode} mode` : '' }}</span>
      </p>
      <div class="files">
        <label class="button">
          Open frames
          <input type="file" accept=".json,application/json" @change="onPick($event, 'frames')" />
        </label>
        <label class="button" :class="{ quiet: !data }">
          {{ audioName ? 'Swap song' : 'Add song' }}
          <input type="file" accept="audio/*" @change="onPick($event, 'audio')" />
        </label>
      </div>
    </header>

    <main v-if="data">
      <section class="stage-wrap">
        <LightStage :channels="data.channels" :colors="colors" :view="view" :show-names="showNames" />
      </section>

      <aside>
        <fieldset>
          <legend>View</legend>
          <label><input v-model="view" type="radio" value="top" /> From above</label>
          <label><input v-model="view" type="radio" value="front" /> From the front</label>
          <label><input v-model="showNames" type="checkbox" /> Light names</label>
        </fieldset>

        <fieldset>
          <legend>Speed</legend>
          <select v-model.number="rate">
            <option :value="0.25">Quarter speed</option>
            <option :value="0.5">Half speed</option>
            <option :value="1">Normal</option>
          </select>
        </fieldset>

        <fieldset :disabled="!audio">
          <legend>Song sync</legend>
          <p v-if="!audio" class="hint">Add the same song to hear it with the lights.</p>
          <template v-else>
            <p class="hint">{{ audioName }}</p>
            <label class="offset">
              <input v-model.number="offsetMs" type="range" min="-3000" max="3000" step="10" />
              <span>{{ offsetMs }} ms</span>
            </label>
            <p class="hint">Raise it if lights hit before the music. [ and ] nudge by 10 ms.</p>
          </template>
        </fieldset>
      </aside>

      <footer>
        <div class="transport">
          <button class="play" :aria-label="playing ? 'Pause' : 'Play'" @click="toggle">
            {{ playing ? 'Pause' : 'Play' }}
          </button>
          <span class="time">{{ clock(time) }} <span class="dim">of {{ clock(data.duration_s) }}</span></span>
          <span class="dim">{{ position }}</span>
        </div>
        <div class="timeline-wrap">
          <SongTimeline :data="data" :time="time" @seek="seek" />
        </div>
      </footer>
    </main>

    <main v-else class="empty">
      <p class="lede">Drop a frames file to watch an effect play back.</p>
      <p class="dim">Make one from the repo root:</p>
      <pre><code>python -m scripts.hue_lab synth -o synth.jsonl
python -m scripts.hue_lab render synth.jsonl --mode energetic</code></pre>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
    </main>
  </div>
</template>

<style scoped>
.shell {
  height: 100%;
  display: flex;
  flex-direction: column;
  padding: 20px 28px 24px;
  gap: 16px;
}
.shell.dragging {
  outline: 2px dashed var(--paper-dim);
  outline-offset: -10px;
}

header {
  display: flex;
  align-items: baseline;
  gap: 20px;
  flex-wrap: wrap;
}
h1 {
  font-size: 1.25rem;
  font-weight: 650;
  margin: 0;
  letter-spacing: -0.01em;
}
.source {
  margin: 0;
  display: flex;
  gap: 12px;
  min-width: 0;
  flex: 1;
}
.source span:first-child {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.files {
  display: flex;
  gap: 8px;
  margin-left: auto;
}
.button {
  border: 1px solid var(--rule);
  border-radius: 999px;
  padding: 6px 14px;
  cursor: pointer;
  font-weight: 500;
}
.button:hover {
  border-color: var(--paper-dim);
}
.button:focus-within {
  outline: 2px solid var(--paper);
  outline-offset: 2px;
}
.button.quiet {
  opacity: 0.6;
}
.button input {
  position: absolute;
  opacity: 0;
  width: 1px;
  height: 1px;
}

main {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 240px;
  grid-template-rows: minmax(0, 1fr) auto;
  gap: 16px 28px;
}
.stage-wrap {
  min-height: 0;
}
aside {
  display: flex;
  flex-direction: column;
  gap: 18px;
}
fieldset {
  border: 0;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
fieldset:disabled {
  opacity: 0.7;
}
legend {
  font-weight: 650;
  margin-bottom: 4px;
}
select {
  background: var(--floor);
  border: 1px solid var(--rule);
  border-radius: 6px;
  padding: 4px 8px;
}
.offset {
  display: flex;
  align-items: center;
  gap: 10px;
}
.offset input {
  flex: 1;
}
.offset span {
  min-width: 64px;
  text-align: right;
}
.hint {
  margin: 0;
  font-size: 0.85rem;
  color: var(--paper-dim);
  line-height: 1.4;
}

footer {
  grid-column: 1 / -1;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.transport {
  display: flex;
  align-items: center;
  gap: 18px;
}
.play {
  background: var(--paper);
  color: var(--night);
  border: 0;
  border-radius: 999px;
  padding: 7px 18px;
  font-weight: 650;
  cursor: pointer;
  min-width: 80px;
}
.time {
  font-weight: 500;
}
.timeline-wrap {
  padding-left: 120px;
  min-width: 0;
}
.dim {
  color: var(--paper-dim);
}

.empty {
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: flex-start;
  max-width: 62ch;
  margin: 0 auto;
  gap: 8px;
}
.lede {
  font-size: 1.6rem;
  font-weight: 500;
  margin: 0 0 8px;
  letter-spacing: -0.01em;
}
.empty p {
  margin: 0;
}
pre {
  background: var(--floor);
  border: 1px solid var(--rule);
  border-radius: 8px;
  padding: 12px 16px;
  margin: 4px 0 0;
  overflow-x: auto;
  font-size: 0.85rem;
}
.error {
  color: var(--warn);
  margin-top: 12px;
}

@media (max-width: 760px) {
  .shell {
    padding: 16px;
  }
  main {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: minmax(260px, 1fr) auto auto;
  }
  footer {
    grid-column: 1;
  }
  .timeline-wrap {
    padding-left: 0;
  }
  .timeline-wrap :deep(.row-names) {
    display: none;
  }
}
</style>
