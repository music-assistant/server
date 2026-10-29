<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { FramesFile } from '../types'

const props = defineProps<{ data: FramesFile; time: number }>()
const emit = defineEmits<{ seek: [seconds: number] }>()

const ENERGY_H = 40
const BEAT_H = 14
const ROW_H = 9
const GAP = 6

const wrap = ref<HTMLDivElement>()
const canvas = ref<HTMLCanvasElement>()
const width = ref(0)

const height = computed(
  () => ENERGY_H + GAP + BEAT_H + GAP + props.data.channels.length * ROW_H,
)
const playheadX = computed(() =>
  props.data.duration_s > 0 ? (props.time / props.data.duration_s) * width.value : 0,
)

function cssVar(name: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

function draw() {
  const el = canvas.value
  const w = width.value
  if (!el || w === 0) return
  const dpr = window.devicePixelRatio || 1
  const h = height.value
  el.width = Math.round(w * dpr)
  el.height = Math.round(h * dpr)
  const ctx = el.getContext('2d')
  if (!ctx) return
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.clearRect(0, 0, w, h)

  const { duration_s: dur, energy, beats, frames, channels } = props.data
  const toX = (s: number) => (s / dur) * w

  // Energy lane: how hard the music is pushing, so effects can be judged against it.
  const peak = Math.max(0.05, ...energy)
  ctx.fillStyle = cssVar('--rule')
  ctx.beginPath()
  ctx.moveTo(0, ENERGY_H)
  for (let x = 0; x < w; x++) {
    const i = Math.floor((x / w) * energy.length)
    ctx.lineTo(x, ENERGY_H - ((energy[i] ?? 0) / peak) * ENERGY_H)
  }
  ctx.lineTo(w, ENERGY_H)
  ctx.closePath()
  ctx.fill()

  const beatTop = ENERGY_H + GAP
  for (const [s, down] of beats) {
    const x = Math.round(toX(s)) + 0.5
    ctx.strokeStyle = down ? cssVar('--paper') : cssVar('--paper-dim')
    ctx.globalAlpha = down ? 0.9 : 0.35
    ctx.beginPath()
    ctx.moveTo(x, beatTop + (down ? 0 : BEAT_H * 0.45))
    ctx.lineTo(x, beatTop + BEAT_H)
    ctx.stroke()
  }
  ctx.globalAlpha = 1

  // One row per light; each column keeps its brightest frame so short flashes stay visible.
  const rowsTop = beatTop + BEAT_H + GAP
  const perCol = frames.length / w
  for (let x = 0; x < w; x++) {
    const start = Math.floor(x * perCol)
    const end = Math.max(start + 1, Math.floor((x + 1) * perCol))
    for (let c = 0; c < channels.length; c++) {
      let best: number[] = [0, 0, 0]
      let bestSum = -1
      for (let f = start; f < end && f < frames.length; f++) {
        const rgb = frames[f][c]
        const sum = rgb[0] + rgb[1] + rgb[2]
        if (sum > bestSum) {
          bestSum = sum
          best = rgb
        }
      }
      ctx.fillStyle = `rgb(${best[0]} ${best[1]} ${best[2]})`
      ctx.fillRect(x, rowsTop + c * ROW_H, 1, ROW_H - 1)
    }
  }
}

let observer: ResizeObserver | undefined
onMounted(() => {
  observer = new ResizeObserver(([entry]) => {
    width.value = Math.floor(entry.contentRect.width)
  })
  if (wrap.value) observer.observe(wrap.value)
})
onBeforeUnmount(() => observer?.disconnect())
watch([width, () => props.data], draw, { flush: 'post' })

let scrubbing = false
function seekFromEvent(e: PointerEvent) {
  const rect = wrap.value?.getBoundingClientRect()
  if (!rect) return
  const ratio = Math.min(Math.max((e.clientX - rect.left) / rect.width, 0), 1)
  emit('seek', ratio * props.data.duration_s)
}
function onDown(e: PointerEvent) {
  scrubbing = true
  ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
  seekFromEvent(e)
}
function onMove(e: PointerEvent) {
  if (scrubbing) seekFromEvent(e)
}
function onUp() {
  scrubbing = false
}
</script>

<template>
  <div
    ref="wrap"
    class="timeline"
    :style="{ height: `${height}px` }"
    role="slider"
    tabindex="0"
    aria-label="Song position"
    :aria-valuemin="0"
    :aria-valuemax="Math.round(data.duration_s)"
    :aria-valuenow="Math.round(time)"
    @pointerdown="onDown"
    @pointermove="onMove"
    @pointerup="onUp"
    @pointercancel="onUp"
  >
    <canvas ref="canvas" :style="{ width: `${width}px`, height: `${height}px` }" />
    <div class="playhead" :style="{ transform: `translateX(${playheadX}px)` }" />
    <div class="row-names" :style="{ top: `${ENERGY_H + BEAT_H + GAP * 2}px` }">
      <span v-for="ch in data.channels" :key="ch.id" :style="{ height: `${ROW_H}px` }">{{ ch.name }}</span>
    </div>
  </div>
</template>

<style scoped>
.timeline {
  position: relative;
  cursor: ew-resize;
  touch-action: none;
  user-select: none;
}
.timeline:focus-visible {
  outline: 2px solid var(--paper);
  outline-offset: 4px;
}
canvas {
  display: block;
}
.playhead {
  position: absolute;
  top: -4px;
  bottom: -4px;
  left: -1px;
  width: 2px;
  background: var(--paper);
  pointer-events: none;
}
.row-names {
  position: absolute;
  right: calc(100% + 10px);
  display: flex;
  flex-direction: column;
  pointer-events: none;
}
.row-names span {
  font-size: 9px;
  line-height: 9px;
  white-space: nowrap;
  color: var(--paper-dim);
  text-align: right;
}
</style>
