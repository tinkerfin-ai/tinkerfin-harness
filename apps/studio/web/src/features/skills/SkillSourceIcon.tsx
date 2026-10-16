import { Globe2 } from 'lucide-react'
import clawHubIcon from '../../assets/skills/clawhub.png'

/** 来源品牌由前端固定维护，新增服务端来源使用通用网站图标 */
export function SkillSourceIcon({ id }: { id: string }) {
  if (id === 'clawhub') return <img className="skills-source-icon" src={clawHubIcon} alt="" aria-hidden="true" />
  const path = id === 'anthropic'
    ? 'M17.3041 3.541h-3.6718l6.696 16.918H24Zm-10.6082 0L0 20.459h3.7442l1.3693-3.5527h7.0052l1.3693 3.5528h3.7442L10.5363 3.5409Zm-.3712 10.2232 2.2914-5.9456 2.2914 5.9456Z'
    : id === 'vercel' ? 'm12 1.608 12 20.784H0Z' : null
  return path
    ? <svg className="skills-source-icon" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d={path} /></svg>
    : <Globe2 className="skills-source-icon" aria-hidden="true" />
}
