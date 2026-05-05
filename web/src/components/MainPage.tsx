import { Header } from './Header/Header'
import { HedgeChart } from './Chart/HedgeChart'
import { HedgeOrderForm } from './OrderForm/HedgeOrderForm'
import { PositionList } from './PositionList/PositionList'

export function MainPage() {
  return (
    <div className="h-screen flex flex-col bg-gray-50 min-w-[1280px]">
      <Header />

      <div className="flex flex-1 min-h-0 p-3 gap-3">
        <div className="flex-[7] min-w-0">
          <HedgeChart />
        </div>
        <div className="w-[380px] flex-shrink-0">
          <HedgeOrderForm />
        </div>
      </div>

      <div className="h-[280px] flex-shrink-0 px-3 pb-3">
        <PositionList />
      </div>
    </div>
  )
}
