export interface AboutLink {
  label: string;
  href: string;
  detail?: string;
}

export interface TimelineItem {
  period: string;
  title: string;
  subtitle?: string;
  description?: string[];
}

export interface ProjectItem {
  name: string;
  summary: string;
  href?: string;
  role?: string;
  tags?: string[];
}

export interface SkillGroup {
  label: string;
  items: string[];
}

export interface MetricItem {
  label: string;
  value: string | number;
  detail?: string;
}
