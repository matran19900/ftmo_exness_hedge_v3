import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { UnmappedExnessSection } from './UnmappedExnessSection'
import { UnmappedFtmoSection } from './UnmappedFtmoSection'

describe('UnmappedFtmoSection', () => {
  it('renders nothing when symbols list is empty', () => {
    const { container } = render(<UnmappedFtmoSection symbols={[]} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders header collapsed by default', () => {
    render(<UnmappedFtmoSection symbols={['SPX500', 'GER40.cash']} />)
    expect(screen.getByText(/2 FTMO symbols without/)).toBeInTheDocument()
    // Symbols should not be visible while collapsed
    expect(screen.queryByText(/SPX500/)).toBeNull()
  })

  it('expands to show symbols on click', async () => {
    render(<UnmappedFtmoSection symbols={['SPX500']} />)
    await userEvent.click(screen.getByText(/1 FTMO symbol without/))
    expect(screen.getByText(/SPX500/)).toBeInTheDocument()
  })

  it('uses singular wording for one symbol', () => {
    render(<UnmappedFtmoSection symbols={['XYZ']} />)
    expect(screen.getByText(/1 FTMO symbol without/)).toBeInTheDocument()
  })
})

describe('UnmappedExnessSection', () => {
  it('renders nothing when symbols list is empty', () => {
    const { container } = render(<UnmappedExnessSection symbols={[]} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders count and is collapsed by default', () => {
    render(<UnmappedExnessSection symbols={['BTCUSDm', 'ETHUSDm']} />)
    expect(screen.getByText(/2 Exness symbols not claimed/)).toBeInTheDocument()
    expect(screen.queryByText('BTCUSDm')).toBeNull()
  })

  it('expand toggle works', async () => {
    render(<UnmappedExnessSection symbols={['BTCUSDm']} />)
    await userEvent.click(screen.getByText(/1 Exness symbol not claimed/))
    expect(screen.getByText('BTCUSDm')).toBeInTheDocument()
  })
})
