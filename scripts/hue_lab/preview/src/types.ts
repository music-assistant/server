export type Rgb = [number, number, number]

export interface LightChannel {
  id: number
  name: string
  /** Hue entertainment coordinates, each -1..1: x left→right, y back→front, z floor→ceiling. */
  pos: [number, number, number]
}

/** Output of `python -m scripts.hue_lab render`. */
export interface FramesFile {
  version: 1
  source: string
  engine: string
  mode: string | null
  fps: number
  channels: LightChannel[]
  duration_s: number
  /** frames[frame][channelIndex] = [r, g, b] 0-255 */
  frames: Rgb[][]
  /** [seconds, 1 if downbeat else 0] */
  beats: [number, number][]
  /** [seconds, strength 0-1] */
  peaks: [number, number][]
  /** Mean spectrum level per received frame, 0-1, evenly spaced over the duration. */
  energy: number[]
}

export type StageView = 'top' | 'front'

export function parseFramesFile(text: string): FramesFile {
  const data = JSON.parse(text) as Partial<FramesFile>
  if (data.version !== 1 || !Array.isArray(data.frames) || !Array.isArray(data.channels)) {
    throw new Error('Not a hue_lab frames file. Render one with: python -m scripts.hue_lab render <recording>')
  }
  return data as FramesFile
}
