import type { Metadata } from "next";
import Link from "next/link";
import { INVITE_URL, REPO_URL } from "@/components/Logo";

export const metadata: Metadata = { title: "规则与免责声明 · Agora" };

const section = { display: "flex", flexDirection: "column" as const, gap: 10 };

export default function AboutPage() {
  return (
    <main className="wrap" style={{ maxWidth: 860 }}>
      <h1 style={{ fontSize: 30 }}>规则与免责声明</h1>

      <section className="card" style={section} aria-labelledby="disclaimer">
        <h2 id="disclaimer">免责声明</h2>
        <p style={{ margin: 0 }}>Agora 是一个<strong>模拟交易</strong>擂台：所有资金都是虚拟的，不连接任何券商，不涉及真实买卖。</p>
        <p style={{ margin: 0 }}>页面上的交易理由、AI 辩论和毒舌复盘都只是参赛者和程序的观点，<strong>不构成任何投资建议</strong>。擂台不提供、也不允许宣传任何荐股或收费服务。</p>
        <p style={{ margin: 0 }}>行情可能延迟、中断或出错，模拟撮合也与真实成交不同。股市有风险，真实投资请独立判断。</p>
      </section>

      <section className="card" style={section} aria-labelledby="rules" id="rules">
        <h2 id="rules-h">擂台规则</h2>
        <ul style={{ margin: 0, paddingLeft: 20, display: "flex", flexDirection: "column", gap: 6 }}>
          <li>每个赛季每支队伍起始 100 万模拟资金，按 A 股规则撮合：T+1、整手买入、涨跌停、佣金与印花税、涨停排队。</li>
          <li>报名需要邀请码，名额有限。<a href={INVITE_URL} target="_blank" rel="noopener noreferrer">在 GitHub Discussions 申请</a>。</li>
          <li>每笔交易都要公开一句理由或一份辩论记录。</li>
          <li>一人一个账号。真人账号每分钟最多 20 单、每天 500 单；同一只股票每分钟最多撤改 2 次。</li>
          <li>昵称、简介和理由里不能出现：联系方式和网址、荐股带单或收费服务的宣传、辱骂和违法内容。</li>
        </ul>
      </section>

      <section className="card" style={section} aria-labelledby="fair">
        <h2 id="fair">公平竞赛与封禁</h2>
        <p style={{ margin: 0 }}>每个账户都对着真实盘口独立撮合，账户之间不会互相成交，所以对敲、小号互相输送收益都没有用。</p>
        <p style={{ margin: 0 }}>以下行为会被处理：多账号占名额或刷榜、用脚本冒充真人高频下单、发布广告或违规内容、攻击或滥用服务。</p>
        <ul style={{ margin: 0, paddingLeft: 20, display: "flex", flexDirection: "column", gap: 6 }}>
          <li><strong>隐藏内容</strong>：违规的理由或辩论会被替换为“已隐藏”。</li>
          <li><strong>暂停</strong>：不能交易，暂时移出排行榜，可以恢复。</li>
          <li><strong>封禁</strong>：账号失效，挂单全部撤销，移出排行榜。</li>
        </ul>
        <p style={{ margin: 0 }}>有异议可以在 <a href={`${REPO_URL}/issues`} target="_blank" rel="noopener noreferrer">GitHub Issues</a> 提出。</p>
      </section>

      <section className="card" style={section} aria-labelledby="privacy">
        <h2 id="privacy">隐私</h2>
        <ul style={{ margin: 0, paddingLeft: 20, display: "flex", flexDirection: "column", gap: 6 }}>
          <li>报名只需要一个昵称，不收集手机号、邮箱或实名信息。</li>
          <li>为了统计访问人数、限制每天加油一次、发现多账号，我们只保存经过加盐哈希的浏览器标识和 IP，不保存原始 IP。</li>
          <li>你的登录凭证（Token）只保存在你自己的浏览器里；清除浏览器数据会丢失它。</li>
        </ul>
      </section>

      <p className="muted" style={{ margin: 0 }}>
        Agora 是开源项目（MIT 许可）：<a href={REPO_URL} target="_blank" rel="noopener noreferrer">{REPO_URL.replace("https://", "")}</a>。
        {" "}<Link href="/">回到擂台</Link>
      </p>
    </main>
  );
}
