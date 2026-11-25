import { onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { errorText, unwrap } from '../api/transport'

type Message = components['schemas']['ConversationMessage']

export function useConversationHistory(anchor: () => string | null, describeError = errorText) {
  const earlierMessages = ref<Message[]>([])
  const earlierCursor = ref<string | null>(null)
  const earlierLoading = ref(false)
  const earlierError = ref('')
  let request: AbortController | undefined

  async function loadEarlier() {
    const before = earlierCursor.value
    if (!before || earlierLoading.value || !anchor()) return
    const { signal } = (request = new AbortController())
    earlierLoading.value = true
    earlierError.value = ''
    try {
      const page = unwrap(
        await api.GET('/api/v1/learning/agent/runs/{run_id}/conversation', {
          params: { path: { run_id: before } },
          signal,
        }),
      )
      if (!signal.aborted) {
        earlierMessages.value = [...page.messages, ...earlierMessages.value]
        earlierCursor.value = page.next_before
      }
    } catch (cause) {
      if (!signal.aborted) earlierError.value = describeError(cause)
    } finally {
      if (!signal.aborted) earlierLoading.value = false
    }
  }

  watch(
    anchor,
    (before) => {
      request?.abort()
      earlierMessages.value = []
      earlierCursor.value = before
      earlierLoading.value = false
      earlierError.value = ''
      if (before) void loadEarlier()
    },
    { immediate: true },
  )
  onBeforeUnmount(() => request?.abort())

  return { earlierMessages, earlierCursor, earlierLoading, earlierError, loadEarlier }
}
