/** Match the scene's bounded context window, not OpenCode's 20K default buffer. */
export const sceneContext = {
  limit: { context: 16384, output: 4096 },
  compaction: { auto: true, reserved: 4096, preserve_recent_tokens: 8000 },
}
