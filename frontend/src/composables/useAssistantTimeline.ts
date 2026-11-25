import { onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { unwrap } from '../api/transport'

type Turn = components['schemas']['AgentConversationRun']

export function useAssistantTimeline(
  session: () => string | undefined,
  activeRun: () => string | undefined,
  describeError: (cause: unknown) => string,
) {
  const timeline = ref<Turn[]>([])
  const conversationId = ref<string>()
  const earlier = ref<string | null>(null)
  const loadingTimeline = ref(false)
  const timelineError = ref('')
  let request: AbortController | undefined

  async function loadTimeline(before?: string) {
    request?.abort()
    const id = session()
    if (!id) return
    const { signal } = (request = new AbortController())
    loadingTimeline.value = true
    timelineError.value = ''
    try {
      const page = unwrap(
        await api.GET('/api/v1/learning/agent/sessions/{session_id}/conversation', {
          params: { path: { session_id: id }, query: { before } },
          signal,
        }),
      )
      if (signal.aborted) return
      conversationId.value = page.conversation_id
      timeline.value = before ? [...page.runs, ...timeline.value] : page.runs
      earlier.value = page.next_before
    } catch (cause) {
      if (!signal.aborted) timelineError.value = describeError(cause)
    } finally {
      if (!signal.aborted) loadingTimeline.value = false
    }
  }

  watch([session, activeRun], ([id], [previous]) => {
    if (id !== previous) {
      conversationId.value = undefined
      timeline.value = []
      earlier.value = null
      loadingTimeline.value = false
      timelineError.value = ''
    }
    void loadTimeline()
  })
  onBeforeUnmount(() => request?.abort())

  return { timeline, conversationId, earlier, loadingTimeline, timelineError, loadTimeline }
}
