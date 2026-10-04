import { computed, ref, watch, type Ref } from 'vue'
export function usePagination<T>(items: Readonly<Ref<T[]>>, initialSize = 10) {
  const page = ref(1)
  const pageSize = ref(initialSize)
  const total = computed(() => items.value.length)
  const paged = computed(() =>
    items.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value),
  )
  watch([items, pageSize], () => {
    page.value = 1
  })
  return { page, pageSize, total, paged }
}
