const params = new URLSearchParams(window.location.search)
export const TOKEN = params.get('token') ?? ''

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('X-OnlyOne-Token', TOKEN)
  if (init.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const res = await fetch(path, { ...init, headers })
  if (!res.ok) {
    let detail = ''
    try {
      detail = await res.text()
    } catch {
      detail = ''
    }
    throw new Error(`${res.status} ${detail || res.statusText}`)
  }
  return (await res.json()) as T
}
