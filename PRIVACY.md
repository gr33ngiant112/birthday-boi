# Privacy

This page describes what birthday-boi stores, who can see it, and how to delete it. It describes the code in this repository; whoever runs a copy of the bot holds that copy's data.

## What the bot stores

When you set your birthday in a server, with `/set_birthday` or by mentioning the bot ("@Birthday Boi my birthday is ..."), the bot stores:

- your Discord user ID,
- your birthday, as the full date with the year,
- the IDs of the servers where you set it.

You have one stored birthday. Setting it again, in any server, changes the date in every server where you set it.

The bot stores nothing else about you: not your name, and not your messages. Nothing expires; the data stays until it is deleted as described below.

## Who sees what

In Discord, only members of a server where you set your birthday can see it, and only in that server:

- `/get_birthday` and `/list_birthdays` show your full date, with the year, to the member who runs them. Only that member sees the answer.
- `/forecast_birthdays` shows the month and day and the age you will turn (and so your birth year), if your birthday is today or in the next 90 days. Only the member who runs it sees the answer.
- On the first day of each month, at 15:00 UTC, the bot posts the birthdays in that month and the next two in the server's #general channel. The post shows the month and day, never the year or age, and everyone who can read #general sees it. So that a server gets one post a month, the bot records the server's ID and the month of its post, and deletes that record after 40 days.
- If you set your birthday by mentioning the bot, everyone who can read that channel sees your message and the bot's reply, which repeats the full date with the year.
- `/set_birthday` answers only you.

Whoever runs the bot can read everything it stores.

## How to delete your birthday

Run `/forget_birthday` in any server the bot shares with you. It deletes your birthday and the list of servers where you set it, and removes you from every server's list, not only the one where you run it. Only you see the reply.

It does not delete Discord messages: if you set your birthday by mentioning the bot, your message and the bot's reply stay in the channel until someone deletes them.

If you no longer share a server with the bot, you cannot run `/forget_birthday`. Ask whoever runs the bot to delete your data.

## When you leave a server

Leaving a server deletes nothing. You stay on that server's list, and `/list_birthdays`, `/forecast_birthdays` and the monthly post there still show your birthday, until you run `/forget_birthday`.

## When the bot leaves a server

When the bot is removed from a server, or the server is deleted, the bot deletes that server's list. If that was the only server where you set your birthday, it also deletes your birthday and your list of servers, because no server can see them any more. Other servers where you set it still see it.

This cleanup runs once, when Discord tells the running bot about the removal, and it is not retried. If the bot is not running at that moment, Discord may never tell it; if the bot cannot reach its database, the cleanup stops. Whatever it did not delete stays until you run `/forget_birthday` or whoever runs the bot deletes it.

## Where the data is kept

Everything the bot stores is kept in the Redis database of whoever runs the bot, at the address in its `REDIS_URL` setting. This repository holds only the code, not any data.

When something goes wrong, the bot prints an error to its log. Some of these messages include user IDs, server IDs or names, and sometimes a stored birthday. The log is kept wherever the person running the bot keeps it.
