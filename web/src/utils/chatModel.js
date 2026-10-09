/**
 * 从当前启用的聊天模型中解析对话实际展示的模型。
 */
export const resolveAvailableChatModelSpec = ({
  selectedModel,
  agentModel,
  systemModel,
  availableSpecs
}) => {
  const normalizedSpecs = Array.isArray(availableSpecs)
    ? availableSpecs.filter((spec) => typeof spec === 'string' && spec)
    : []
  const availableSet = new Set(normalizedSpecs)

  for (const candidate of [selectedModel, agentModel, systemModel]) {
    if (availableSet.has(candidate)) return candidate
  }

  return normalizedSpecs[0] || ''
}
