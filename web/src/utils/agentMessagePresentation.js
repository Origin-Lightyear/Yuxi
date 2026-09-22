/**
 * 根据整轮处理状态和推理内容决定推理面板的默认展示状态。
 */
export const resolveReasoningPresentation = (
  {
    isProcessing = false,
    status = '',
    hasReasoning = false,
    hasContent = false,
    hasToolCalls = false
  } = {}
) => {
  const isTerminal = [
    'finished',
    'success',
    'error',
    'failed',
    'interrupted',
    'cancelled',
    'completed'
  ].includes(status)
  const active = isProcessing && !isTerminal && hasReasoning && !hasContent && !hasToolCalls
  return { active, expanded: active }
}

/**
 * 为工具调用选择唯一的自动展开项，完成项默认保持收起。
 */
export const resolveToolPresentation = (toolCalls = [], activeIds = new Set(), isActive = true) => {
  const activeIndexes = toolCalls.reduce((indexes, toolCall, index) => {
    const id = toolCall?.id == null ? '' : String(toolCall.id)
    const isTerminal = [
      'success',
      'finished',
      'error',
      'failed',
      'interrupted',
      'cancelled',
      'completed'
    ].includes(toolCall?.status)
    if (isActive && !isTerminal && (activeIds.has(id) || toolCall?.status === 'running')) {
      indexes.push(index)
    }
    return indexes
  }, [])
  const activeIndex = activeIndexes.at(-1)

  return toolCalls.map((toolCall, index) => ({
    ...toolCall,
    active: index === activeIndex,
    expanded: index === activeIndex
  }))
}
