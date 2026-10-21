import { create } from 'zustand'
import apiClient from '../api/client'
import desktopContext from '../api/context'
import { mergeMenu } from '../lib/menu'
import type { MenuDoc, MenuItem } from '../types'

interface MenuState {
  /** The menu the user saved; null while they keep the built-in one. */
  saved: MenuDoc | null
  /** What the rail draws: the saved menu merged with this version's built-in pages. */
  doc: MenuDoc
  /** The backend has answered at least once, so a missing entry really is missing. */
  loaded: boolean
  availableViews: Record<string, boolean>
  error: string
  load: () => Promise<void>
  /** Save the whole menu (null restores the built-in one). Throws with the backend's reason. */
  save: (menu: MenuDoc | null) => Promise<void>
  editorOpen: boolean
  /** What the editor opens on: an entry to place in the first group, or one to point out. */
  editorIntent: EditorIntent | null
  openEditor: (intent?: EditorIntent) => void
  closeEditor: () => void
}

export interface EditorIntent {
  add?: MenuItem
  focus?: string
}

const apply = (menu: MenuDoc | null, availableViews: Record<string, boolean>) => ({
  saved: menu, doc: mergeMenu(menu), availableViews, loaded: true, error: '',
})

// The server stores per-member layouts. Do not reuse the legacy global browser cache.
export const useMenuStore = create<MenuState>((set) => ({
  saved: null, doc: mergeMenu(null), loaded: false, availableViews: {}, error: '',
  load: async () => {
    const revision = desktopContext.getSnapshot().featureRevision
    try {
      const res = await apiClient.getMenu()
      if (revision !== desktopContext.getSnapshot().featureRevision) return
      if (res.status !== 'success') throw new Error(res.message || 'Menu unavailable')
      set(apply(res.menu, res.available_views || {}))
    } catch (error) {
      if (revision === desktopContext.getSnapshot().featureRevision)
        set({ loaded: true, availableViews: {}, error: String(error) })
    }
  },
  save: async (menu) => {
    const revision = desktopContext.getSnapshot().featureRevision
    const res = await apiClient.saveMenu(menu)
    if (revision !== desktopContext.getSnapshot().featureRevision) return
    if (res.status !== 'success') throw new Error(res.message || 'Menu unavailable')
    set(apply(res.menu, res.available_views || {}))
  },

  editorOpen: false,
  editorIntent: null,
  openEditor: (intent) => set({ editorOpen: true, editorIntent: intent || null }),
  closeEditor: () => set({ editorOpen: false, editorIntent: null }),
}))

let revision = desktopContext.getSnapshot().featureRevision
desktopContext.subscribe(() => {
  const snapshot = desktopContext.getSnapshot()
  if (snapshot.featureRevision === revision) return
  revision = snapshot.featureRevision
  useMenuStore.setState({ saved: null, doc: mergeMenu(null), loaded: false, availableViews: {},
    editorOpen: false, editorIntent: null, error: '' })
  if (snapshot.gate === 'ready') void useMenuStore.getState().load()
})
