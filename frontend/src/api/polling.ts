export function boundedPoll<T>(options: {
  load: (signal: AbortSignal) => Promise<T>
  terminal: (value: T) => boolean
  result: (value: T) => void
  error: (error: unknown) => void
  exhausted: () => void
  interval?: number
  maxRequests?: number
  maxTime?: number
}) {
  let timer: ReturnType<typeof setTimeout> | undefined
  let request: AbortController | undefined
  let generation = 0
  let disposed = false
  let visible = true
  let active = false
  let count = 0
  let deadline = 0
  function stop() {
    generation++
    clearTimeout(timer)
    request?.abort()
    request = undefined
  }
  async function tick() {
    if (!active || !visible || disposed) return
    if (count >= (options.maxRequests ?? 240) || Date.now() >= deadline) {
      active = false
      options.exhausted()
      return
    }
    const current = generation
    const controller = new AbortController()
    request = controller
    count++
    try {
      const value = await options.load(controller.signal)
      if (current !== generation || disposed) return
      options.result(value)
      if (options.terminal(value)) active = false
      if (active) timer = setTimeout(tick, options.interval ?? 2000)
    } catch (error) {
      if (current !== generation || disposed) return
      active = false
      options.error(error)
    } finally {
      if (request === controller) request = undefined
    }
  }
  return {
    start() {
      if (disposed) return
      stop()
      active = true
      count = 0
      deadline = Date.now() + (options.maxTime ?? 600000)
      void tick()
    },
    visible(value: boolean) {
      if (disposed || visible === value) return
      visible = value
      stop()
      if (visible) void tick()
    },
    dispose() {
      disposed = true
      active = false
      stop()
    },
  }
}
