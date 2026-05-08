import { HedgeChart } from './Chart/HedgeChart'
import { Header } from './Header/Header'
import { HedgeOrderForm } from './OrderForm/HedgeOrderForm'
import { PositionList } from './PositionList/PositionList'

export function MainPage() {
  return (
    <div className="h-screen flex flex-col bg-gray-50 min-w-[1280px]">
      <Header />

      <div className="flex-1 flex min-h-0 overflow-hidden">
        {/* Left 70%: chart on top, position list below. */}
        <div className="w-[70%] flex flex-col gap-2 p-2 pr-1 min-h-0 overflow-hidden">
          <div className="flex-1 min-h-0">
            <HedgeChart />
          </div>
          <div className="h-[35%] min-h-0">
            <PositionList />
          </div>
        </div>

        {/* Right 30%: order form full-height column. */}
        <div className="w-[30%] p-2 pl-1 min-h-0 overflow-hidden">
          <HedgeOrderForm />
        </div>
      </div>
    </div>
  )
}
