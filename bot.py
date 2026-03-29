def format_site_entry(r) -> str:
    _, url, title, _, __, is_up, last_changed, last_diff = r
    status = "[ok]" if is_up else "[!!]"
    label = (title or extract_domain(url))[:35]
    
    # Делаем название сайта гиперссылкой
    res = f"{status} <a href='{url}'>{label}</a>"
    
    if not is_up:
        res += f"\n└ status: <i>error</i>"
    elif last_changed:
        res += f"\n└ updated: {last_changed}"
        if last_diff: 
            res += f"\n└ diff: {last_diff}"
    else:
        res += f"\n└ status: <i>idle</i>"
    return res

async def show_list(message: types.Message, page: int = 0, edit: bool = False):
    rows = await get_urls()
    if not rows:
        if edit: await message.edit_text("list is empty.")
        else: await message.answer("list is empty.", reply_markup=main_keyboard())
        return

    total_pages = (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, total_pages - 1))
    start, end = page * PAGE_SIZE, (page + 1) * PAGE_SIZE
    
    up = sum(1 for r in rows if r[5])
    header = f"~~ <b>mzekali's watch-list</b> ({len(rows)})\n(+) {up} up  |  (!!) {len(rows)-up} down\n\n"
    body = "\n\n".join(format_site_entry(r) for r in rows[start:end])
    
    kb = list_view_keyboard(page, total_pages)
    
    # ОБЯЗАТЕЛЬНО: disable_web_page_preview=True, чтобы не было гигантских превью
    if edit:
        try:
            await message.edit_text(header + body, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        except Exception: # На случай если контент не изменился
            pass
    else:
        await message.answer(header + body, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
