import {beginDiagnostic} from '../debug/diagnostics'
export const API = import.meta.env.VITE_API_URL ?? 'http://localhost:8000/api/v1'
export type Project = {id:string; name:string; state:'draft'|'approved'; created_at:string}
export type Document = {id:string;project_id:string;kind:'tu'|'znp'|'other';filename:string;created_at:string}
export type Fragment = {id:string;document_id:string;page:number;position:number;text:string;block_type?:string;extraction_method?:'ocr'|'text';section_path?:string|null}
export type Evidence = {document_id:string;fragment_id?:string|null;page?:number|null;quote:string}
export type Finding = {id:string;project_id:string;module:string;type:string;severity:string;explanation:string;suggested_action:string|null;evidence:Evidence[];review_status:'pending'|'accepted'|'rejected'}
export type Normative = {id:string;code:string;title:string;status:'active'|'obsolete'|'unknown';replacement?:string|null;source_url?:string|null;registry_status?:string|null;checked_at?:string|null;updated_at?:string|null}

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
