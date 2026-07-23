import { redirect } from 'next/navigation'

export default async function TradePage({
  searchParams,
}: {
  searchParams: Promise<{ case?: string | string[]; ticker?: string | string[] }>
}) {
  const params = await searchParams
  const caseValue = Array.isArray(params.case) ? params.case[0] : params.case
  const tickerValue = Array.isArray(params.ticker) ? params.ticker[0] : params.ticker
  if (caseValue || tickerValue) {
    const query = new URLSearchParams()
    if (caseValue) query.set('case', caseValue)
    if (tickerValue) query.set('ticker', tickerValue)
    redirect(`/decision?${query.toString()}`)
  }
  redirect('/')
}
