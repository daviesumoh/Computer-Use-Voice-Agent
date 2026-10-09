-- The owner can answer a task's question and add to a running task by text or by voice.
-- Both are instructions with a body, applied at the task's next safe point like pause/resume.

alter table public.instructions
  add column channel text not null default 'sms' check (channel in ('sms', 'voice', 'dashboard')),
  add column body text check (char_length(body) <= 1000);

alter table public.instructions drop constraint instructions_command_check;
alter table public.instructions add constraint instructions_command_check
  check (command in ('pause', 'resume', 'cancel', 'edit', 'answer'));
alter table public.instructions add constraint instructions_body_matches_command
  check ((command in ('edit', 'answer')) = (body is not null));
