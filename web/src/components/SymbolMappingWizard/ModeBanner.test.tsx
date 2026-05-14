import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ModeBanner } from './ModeBanner'

describe('ModeBanner', () => {
  it('renders nothing when mode is null', () => {
    const { container } = render(
      <ModeBanner
        mode={null}
        accountId="exness_001"
        fuzzyScore={null}
        fuzzySource={null}
        sharedAccountCount={1}
      />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('renders create mode title + account_id', () => {
    render(
      <ModeBanner
        mode="create"
        accountId="exness_001"
        fuzzyScore={null}
        fuzzySource={null}
        sharedAccountCount={1}
      />,
    )
    expect(screen.getByText(/Create Symbol Mapping/i)).toBeInTheDocument()
    expect(screen.getByText(/exness_001/)).toBeInTheDocument()
  })

  it('renders diff mode score + source', () => {
    render(
      <ModeBanner
        mode="diff"
        accountId="exness_002"
        fuzzyScore={0.97}
        fuzzySource="exness_other_abc.json"
        sharedAccountCount={1}
      />,
    )
    expect(screen.getByText(/Diff-Aware Mode/)).toBeInTheDocument()
    expect(screen.getByText(/97%/)).toBeInTheDocument()
    expect(screen.getByText(/exness_other_abc.json/)).toBeInTheDocument()
  })

  it('renders spec_mismatch mode warning', () => {
    render(
      <ModeBanner
        mode="spec_mismatch"
        accountId="exness_003"
        fuzzyScore={null}
        fuzzySource={null}
        sharedAccountCount={1}
      />,
    )
    expect(screen.getByText(/Spec Mismatch Detected/)).toBeInTheDocument()
    expect(screen.getByText(/divergent contract specs/)).toBeInTheDocument()
  })

  it('edit mode banner shows shared account warning singular', () => {
    render(
      <ModeBanner
        mode="edit"
        accountId="exness_001"
        fuzzyScore={null}
        fuzzySource={null}
        sharedAccountCount={1}
      />,
    )
    expect(screen.getByText(/shared with 1 account/)).toBeInTheDocument()
    expect(screen.getByText(/changes apply to all/)).toBeInTheDocument()
  })

  it('edit mode banner uses plural correctly', () => {
    render(
      <ModeBanner
        mode="edit"
        accountId="exness_001"
        fuzzyScore={null}
        fuzzySource={null}
        sharedAccountCount={3}
      />,
    )
    expect(screen.getByText(/shared with 3 accounts/)).toBeInTheDocument()
  })

  it('writes data-mode attribute for downstream styling', () => {
    render(
      <ModeBanner
        mode="create"
        accountId="exness_001"
        fuzzyScore={null}
        fuzzySource={null}
        sharedAccountCount={1}
      />,
    )
    expect(screen.getByTestId('wizard-mode-banner').dataset.mode).toBe('create')
  })
})
