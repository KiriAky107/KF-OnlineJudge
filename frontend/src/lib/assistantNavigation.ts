import type { LocationQuery, RouteLocationRaw } from 'vue-router'

export const assistantRouteName = 'learning-assistant'

export function assistantPage(
  query: LocationQuery = {},
  contextProblem?: string,
  contextTraining?: string,
): RouteLocationRaw {
  const { helpRun, trainingRun, assistantTab } = query
  return {
    name: assistantRouteName,
    query: {
      run: helpRun,
      trainingRun,
      assistantTab,
      problem: contextProblem,
      training: contextTraining,
    },
  }
}
