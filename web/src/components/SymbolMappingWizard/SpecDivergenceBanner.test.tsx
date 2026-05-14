import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { SpecDivergenceBanner } from './SpecDivergenceBanner'

describe('SpecDivergenceBanner', () => {
  it('renders nothing when divergences is empty', () => {
    const { container } = render(<SpecDivergenceBanner divergences={[]} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders count of blocking + warning divergences', () => {
    render(
      <SpecDivergenceBanner
        divergences={[
          { symbol: 'EURUSD', field: 'contract_size', cached_value: 100000, raw_value: 10000, severity: 'BLOCK', delta_percent: null },
          { symbol: 'EURUSD', field: 'pip_size', cached_value: 0.0001, raw_value: 0.000106, severity: 'WARN', delta_percent: 6.0 },
        ]}
      />,
    )
    expect(screen.getByText(/1 blocking divergence/)).toBeInTheDocument()
    expect(screen.getByText(/1 warning/)).toBeInTheDocument()
    expect(screen.getByText(/EURUSD.contract_size/)).toBeInTheDocument()
  })

  it('shows delta_percent when present', () => {
    render(
      <SpecDivergenceBanner
        divergences={[
          { symbol: 'XAUUSD', field: 'pip_value', cached_value: 10, raw_value: 11.5, severity: 'WARN', delta_percent: 15.0 },
        ]}
      />,
    )
    expect(screen.getByText(/Δ15.0%/)).toBeInTheDocument()
  })

  it('uses BLOCK label tag in list items', () => {
    render(
      <SpecDivergenceBanner
        divergences={[
          { symbol: 'XAUUSD', field: 'currency_profit', cached_value: 'USD', raw_value: 'EUR', severity: 'BLOCK', delta_percent: null },
        ]}
      />,
    )
    expect(screen.getByText(/\[BLOCK\]/)).toBeInTheDocument()
  })
})
