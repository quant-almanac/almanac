import { NextResponse } from 'next/server'

// The execution ledger is the canonical history surface.  Preserve old
// bookmarks without retaining a second, independently drifting UI.
export function GET(request: Request) {
  return NextResponse.redirect(new URL('/executions', request.url), 308)
}
