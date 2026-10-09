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
