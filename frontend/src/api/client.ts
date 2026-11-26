import {beginDiagnostic} from '../debug/diagnostics'
export const API = import.meta.env.VITE_API_URL ?? 'http://localhost:8000/api/v1'
export type Project = {id:string; name:string; state:'draft'|'approved'; created_at:string}
export type Document = {id:string;project_id:string;kind:'tu'|'znp'|'other';filename:string;created_at:string;pagination_kind?:'physical'|'logical'|'unknown';normative_extraction_completed?:boolean}
export type Fragment = {id:string;document_id:string;page:number;position:number;text:string;block_type?:string;extraction_method?:'ocr'|'text';section_path?:string|null}
export type Evidence = {document_id:string;znp_document_id?:string|null;fragment_id?:string|null;page?:number|null;quote:string;requirement_id?:string|null}
export type Finding = {id:string;project_id:string;module:string;type:string;severity:string;explanation:string;suggested_action:string|null;evidence:Evidence[];review_status:'pending'|'accepted'|'rejected'}
export type Normative = {id:string;code:string;title:string;status:'active'|'obsolete'|'unknown';replacement?:string|null;source_url?:string|null;registry_status?:string|null;checked_at?:string|null;updated_at?:string|null}

export type RequirementStatus = 'fulfilled' | 'missing' | 'needs_review'
export type RequirementCategory =
  | 'power'
  | 'reliability'
  | 'voltage'
  | 'connection_point'
  | 'metering'
  | 'protection'
  | 'equipment'
  | 'other'

export type TURequirement = {
  id: string
  clause_number: string
  category: RequirementCategory
  text: string
  fragment_id?: string | null
  page?: number | null
  fragment_text?: string | null
}

export type ZNPClause = {
  id: string
  clause_number: string
  text: string
  fragment_id?: string | null
  page?: number | null
  fragment_text?: string | null
}

export type RequirementCheckItem = {
  id: string
  requirement: TURequirement
  status: RequirementStatus
  automatic_status?: RequirementStatus | null
  confidence_score: number
  matched_clause?: ZNPClause | null
  tu_quote?: string | null
  znp_quote?: string | null
  reasoning?: string | null
  is_confirmed_by_human: boolean
  human_comment?: string | null
  human_override_status?: RequirementStatus | null
  tu_fragment_id?: string | null
  tu_page?: number | null
  tu_fragment_text?: string | null
  znp_fragment_id?: string | null
  znp_page?: number | null
  znp_fragment_text?: string | null
  channels?: string[]
  method?: string | null
  validation_details?: {
    version?: number
    pipeline?: string
    matches?: Array<{
      atom_id: string; clause_number: string; relation: string; quote: string
      reasoning?: string; page?: number|null; fragment_id?: string|null
      confidence?: number|null; param_check?: {mismatches?: string[]}
      atom_text?: string; tu_quote?: string; source_phase?: string
      search_score?: number|null
    }>
    obligations?: Array<{id: string; text: string; context: string}>
    obligation_coverage?: Array<{
      obligation_id: string; text: string; status: string; reasoning: string
      evidence_ids?: string[]; checked?: boolean
    }>
    contradiction_audit?: {
      candidates_checked: number
      confirmed: Array<{atom_id: string; clause_number: string; quote: string; reasoning?: string}>
      uncertain_atom_ids: string[]
    }
    primary_candidate_evaluations?: number
    ai_model_evaluations?: number
    validated_links?: number
    targeted_diagnostics?: {
      candidates: number; candidates_total?: number; checked: boolean
      skip_reason?: string|null
      checked_candidates?: number; unchecked_candidates?: number
      truncated_responses?: number
    }
    candidate_limit_reached?: boolean
    model_error?: string|null
    model_status?: string; model_reasoning?: string; items?: Array<{
    item_id: number; text: string; status: string
    evidence: Array<{document_id?: string; fragment_id?: string; clause_number: string; quote: string; page?: number | null}>
  }>}|null
}

export async function request<T>(path:string, options:RequestInit={}):Promise<T> {
  const method = options.method ?? 'GET'
  const operation = beginDiagnostic(`${method} ${path.split('?')[0]}`, 'Запрос отправлен')
  try {
    const response = await fetch(`${API}${path}`,options)
    if(!response.ok) {
      let message = `${response.status}: ${response.statusText}`
      try { const body = await response.json(); message=body.detail ?? message } catch { /* non-JSON error */ }
      throw new Error(typeof message==='string' ? message : JSON.stringify(message))
    }
    if (response.status === 204) {
      operation.success(`HTTP ${response.status}`)
      return undefined as T
    }
    const result = await response.json() as T
    operation.success(`HTTP ${response.status}`)
    return result
  } catch (error) {
    operation.error(error instanceof Error ? error.message : String(error))
    throw error
  }
}
export const json = (method:string, body:unknown):RequestInit=>({method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
