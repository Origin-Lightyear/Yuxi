/**
 * 根据消息终态决定推理面板的默认展示状态。
 */
export const resolveReasoningPresentation = (message = {}) => {
  const active = message.status === 'reasoning'
  return { active, expanded: active }
}

/**
 * 为工具调用选择唯一的自动展开项，完成项默认保持收起。
 */
export const resolveToolPresentation = (toolCalls = [], activeIds = new Set()) => {
  const activeIndexes = toolCalls.reduce((indexes, toolCall, index) => {
    const id = toolCall?.id == null ? '' : String(toolCall.id)
    const isTerminal = ['success', 'finished', 'error', 'cancelled', 'completed'].includes(
      toolCall?.status
    )
    if (!isTerminal && (toolCall?.status === 'running' || activeIds.has(id))) indexes.push(index)
    return indexes
  }, [])
  const activeIndex = activeIndexes.at(-1)

  return toolCalls.map((toolCall, index) => ({
    ...toolCall,
    active: index === activeIndex,
    expanded: index === activeIndex
  }))
}
