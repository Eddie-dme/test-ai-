(async () => {
  await refreshRoles();
  const role = state.roles.find(r => r.name === 'Seraphine') || state.roles[0];

  const sel = document.querySelector('#modeSel');
  sel.value = 'epic';
  sel.dispatchEvent(new Event('change'));

  await startChat(role);

  const turns = [
    "A hundred years on this tower. Does the silence ever wear you down?",
    "Then let me stay tonight — just until the storm passes."
  ];
  for (const t of turns) {
    document.querySelector('#input').value = t;
    await send();
    for (let i = 0; i < 160 && state.sending; i++) {
      await new Promise(r => setTimeout(r, 250));
    }
    await new Promise(r => setTimeout(r, 900));
  }

  const img = document.querySelector('#heroFace');
  if (img.complete && img.naturalWidth > 0) img.style.display = 'block';

  const chat = document.querySelector('#chat');
  chat.scrollTop = chat.scrollHeight;
  await new Promise(r => setTimeout(r, 700));

  return '聊天页: 消息数=' + document.querySelectorAll('#chat .msg').length +
         ' 立绘=' + getComputedStyle(img).display +
         ' 好感度=' + (document.querySelector('#affBadge') || {}).textContent;
})()
