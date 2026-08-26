import { NavLink, Route, Routes } from "react-router-dom";
import { GlobalNotices } from "./components/DataNotices";
import { MapPage } from "./pages/MapPage";
import { RankingPage } from "./pages/RankingPage";
import { ComparePage } from "./pages/ComparePage";
import { AboutPage } from "./pages/AboutPage";
import { ReportsPage } from "./pages/ReportsPage";
import { SignIn } from "./components/SignIn";

export default function App() {
  return (
    <div className="app">
      <header className="app__header">
        <div className="app__brand">
          🦷 <span>Dental Location Analyzer</span>
          <small>歯科開業候補地分析（MVP）</small>
        </div>
        <nav>
          <NavLink to="/" end>
            地図・候補地分析
          </NavLink>
          <NavLink to="/ranking">ランキング</NavLink>
          <NavLink to="/compare">候補地比較</NavLink>
          <NavLink to="/reports">マイレポート</NavLink>
          <NavLink to="/about">データソース・注意事項</NavLink>
        </nav>
        <SignIn />
      </header>

      <GlobalNotices />

      <main className="app__main">
        <Routes>
          <Route path="/" element={<MapPage />} />
          <Route path="/ranking" element={<RankingPage />} />
          <Route path="/compare" element={<ComparePage />} />
          <Route path="/reports" element={<ReportsPage />} />
          <Route path="/about" element={<AboutPage />} />
        </Routes>
      </main>
    </div>
  );
}
