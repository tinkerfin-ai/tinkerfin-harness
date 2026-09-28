export interface SkillSource { id: string; name: string; url: string }
export interface RemoteSkill {
  id: string; source_id: string; name: string; description: string; revision: string | null
  author: string | null; topics: string[]; updated_at: string | null
}
export interface RemoteSkillPage { items: RemoteSkill[]; cursor: string | null }
export interface InstalledSkill {
  source_kind: 'catalog' | 'github' | 'zip'
  id: string; name: string; description: string; source_id: string | null; source_name: string
  external_id: string | null; enabled: boolean; author: string | null; topics: string[]
  file_count: number; byte_size: number; created_at: string; updated_at: string
}
export interface SkillDetail {
  name: string; description: string; markdown: string; files: string[]
  author: string | null; source_url: string | null; topics: string[]
}
export interface RemoteSkillDetail { skill: RemoteSkill; detail: SkillDetail }
export interface ImportCandidate { digest: string; name: string; description: string; file_count: number; byte_size: number }
export interface ImportPreview { id: string; source: 'github' | 'zip'; candidates: ImportCandidate[] }
export interface SkillReplacement { draft_id: string; digest: string }
export interface SkillChangeResult { installation: InstalledSkill; changed: boolean }
export type SkillView = 'discover' | 'mine'
export type SkillSort = 'updated' | 'name'
export type SkillStatus = 'all' | 'enabled' | 'disabled'
export interface SkillFilters { query: string; sort: SkillSort; status: SkillStatus; category: string }
export const emptySkillFilters: SkillFilters = { query: '', sort: 'updated', status: 'all', category: '' }
export type SkillSelection = { kind: 'remote'; skill: RemoteSkill } | { kind: 'installed'; skill: InstalledSkill }
