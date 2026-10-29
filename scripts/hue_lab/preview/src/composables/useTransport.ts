import { onBeforeUnmount, ref, watch, type Ref } from 'vue'

/**
 * Playback clock for the preview. Follows the audio element when one is loaded
 * (so lights stay locked to what you hear), otherwise runs on its own.
 *
 * `offsetMs` is where the frames' t=0 sits in the audio file; raise it if the
 * lights fire early.
 */
export function useTransport(
  duration: Ref<number>,
  audio: Ref<HTMLAudioElement | null>,
  offsetMs: Ref<number>,
) {
  const time = ref(0)
  const playing = ref(false)
  const rate = ref(1)

  let raf = 0
  let last = 0

  const clamp = (t: number) => Math.min(Math.max(t, 0), duration.value)

  function tick(now: number) {
    const el = audio.value
    if (el) {
      time.value = clamp(el.currentTime - offsetMs.value / 1000)
      if (el.ended) playing.value = false
    } else {
      time.value = clamp(time.value + ((now - last) / 1000) * rate.value)
    }
    last = now
    if (time.value >= duration.value && !el) playing.value = false
    if (playing.value) raf = requestAnimationFrame(tick)
  }

  function syncAudio() {
    const el = audio.value
    if (!el) return
    el.playbackRate = rate.value
    el.currentTime = Math.max(0, time.value + offsetMs.value / 1000)
  }

  function play() {
    if (time.value >= duration.value) time.value = 0
    playing.value = true
    syncAudio()
    void audio.value?.play()
    last = performance.now()
    cancelAnimationFrame(raf)
    raf = requestAnimationFrame(tick)
  }

  function pause() {
    playing.value = false
    audio.value?.pause()
    cancelAnimationFrame(raf)
  }

  function toggle() {
    if (playing.value) pause()
    else play()
  }

  function seek(t: number) {
    time.value = clamp(t)
    syncAudio()
  }

  watch(rate, syncAudio)
  watch(offsetMs, () => {
    if (!playing.value) return
    syncAudio()
  })
  watch(audio, (el, prev) => {
    prev?.pause()
    if (el) syncAudio()
    if (playing.value && el) void el.play()
  })

  onBeforeUnmount(() => cancelAnimationFrame(raf))

  return { time, playing, rate, play, pause, toggle, seek }
}
