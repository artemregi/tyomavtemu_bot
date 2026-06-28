-- Запустить в Supabase → SQL Editor → Run

create table if not exists public.bot_users (
  tg_id          text primary key,
  tg_user        text,
  tg_name        text,
  first_seen     timestamptz default now(),
  test_opened_at timestamptz,   -- когда нажал кнопку «Пройти тест»
  test_passed_at timestamptz,   -- когда вернулся из теста с passed_<id>
  is_subscribed  boolean default true
);

-- Только сервис-роль может читать и обновлять; anon — нет
alter table public.bot_users enable row level security;

-- Политика для service_role (бот) — полный доступ
create policy "service_role full access"
  on public.bot_users
  for all
  to service_role
  using (true)
  with check (true);

-- Индекс для быстрых рассылок по статусу
create index if not exists idx_bot_users_subscribed on public.bot_users (is_subscribed);
create index if not exists idx_bot_users_passed     on public.bot_users (test_passed_at) where test_passed_at is not null;
