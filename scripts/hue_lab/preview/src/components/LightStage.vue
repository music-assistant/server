<script setup lang="ts">
import { computed } from 'vue'
import type { LightChannel, Rgb, StageView } from '../types'

const props = defineProps<{
  channels: LightChannel[]
  colors: Rgb[] | null
  view: StageView
  showNames: boolean
}>()

const edges = computed(() =>
  props.view === 'top'
    ? { top: 'Front', bottom: 'Back' }
    : { top: 'Ceiling', bottom: 'Floor' },
)

const lights = computed(() =>
  props.channels.map((ch, i) => {
    const [x, y, z] = ch.pos
    const rgb = props.colors?.[i] ?? [0, 0, 0]
    // Perceived brightness drives bloom size, so flashes read as the light "opening up".
    const lum = (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255
    return {
      key: ch.id,
      name: ch.name,
      cx: x,
      cy: props.view === 'top' ? -y : -z,
      color: `rgb(${rgb[0]} ${rgb[1]} ${rgb[2]})`,
      lum,
      bloom: 0.22 + 0.42 * Math.sqrt(lum),
    }
  }),
)
</script>

<template>
  <svg class="stage" viewBox="-1.45 -1.3 2.9 2.6" role="img" :aria-label="`${view} view of ${channels.length} lights`">
    <defs>
      <pattern id="grid" width="0.25" height="0.25" patternUnits="userSpaceOnUse" x="-1" y="-1">
        <path d="M 0.25 0 L 0 0 0 0.25" class="grid-line" />
      </pattern>
      <radialGradient v-for="l in lights" :id="`bloom-${l.key}`" :key="l.key">
        <stop offset="0" :stop-color="l.color" stop-opacity="1" />
        <stop offset="0.35" :stop-color="l.color" :stop-opacity="0.55 * Math.min(1, l.lum * 3)" />
        <stop offset="1" :stop-color="l.color" stop-opacity="0" />
      </radialGradient>
    </defs>

    <rect x="-1" y="-1" width="2" height="2" class="room" />
    <rect x="-1" y="-1" width="2" height="2" fill="url(#grid)" />
    <text x="0" y="-1.1" class="edge">{{ edges.top }}</text>
    <text x="0" y="1.18" class="edge">{{ edges.bottom }}</text>

    <g class="blooms">
      <circle v-for="l in lights" :key="l.key" :cx="l.cx" :cy="l.cy" :r="l.bloom" :fill="`url(#bloom-${l.key})`" />
    </g>
    <g>
      <circle v-for="l in lights" :key="l.key" :cx="l.cx" :cy="l.cy" r="0.035" :fill="l.color" class="bulb">
        <title>{{ l.name }}</title>
      </circle>
    </g>
    <g v-if="showNames">
      <text v-for="l in lights" :key="l.key" :x="l.cx" :y="l.cy + 0.11" class="name">{{ l.name }}</text>
    </g>
  </svg>
</template>

<style scoped>
.stage {
  width: 100%;
  height: 100%;
  display: block;
}
.room {
  fill: var(--floor);
  stroke: var(--rule);
  stroke-width: 0.006;
}
.grid-line {
  fill: none;
  stroke: var(--rule);
  stroke-width: 0.003;
}
.blooms {
  mix-blend-mode: screen;
}
.bulb {
  stroke: var(--paper-dim);
  stroke-width: 0.006;
}
.edge,
.name {
  fill: var(--paper-dim);
  text-anchor: middle;
  font-size: 0.055px;
}
.name {
  font-size: 0.045px;
}
</style>
