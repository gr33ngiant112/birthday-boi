# Privacy

This page describes what birthday-boi stores, who can see it, and how to delete it. It describes the code in this repository; whoever runs a copy of the bot holds that copy's data.

## What the bot stores

When you set your birthday in a server with `/birthday set`, the bot stores:

- your Discord user ID,
- your birthday: the month and day, and the year if you give one,
- whether you turned on `show_age`, which lets others see your birth year and age,
- the IDs of the servers where you set it.

You have one stored birthday. Setting it again, in any server, changes the date and your `show_age` choice in every server where you set it.

The bot stores nothing else about you: not your name, and not your messages. Nothing expires; the data stays until it is deleted as described below.

## Who sees what

In Discord, only members of a server where you set your birthday can see it, and only in that server:

- `/birthday show` and `/birthday list` show the month and day of your birthday to the member who runs them. If you gave the year and turned on `show_age`, `/birthday show` also shows your birth year and the age you will turn, and `/birthday list` your birth year. Only that member sees the answer.
- `/birthday upcoming` shows the month and day if your birthday is today or within the days the member asks for (30 unless they choose 1 to 366), and the age you will turn only if you gave the year and turned on `show_age`. Only the member who runs it sees the answer.
- On the first day of each month, at 15:00 UTC, the bot posts the birthdays in that month and the next two in the server's #general channel. The post shows the month and day, never the year or age, and everyone who can read #general sees it. So that a server gets one post a month, the bot records the server's ID and the month of its post, and deletes that record after 40 days.
- `/birthday set` answers only you.

Before `/birthday set`, the year was required, so a birthday set then has one. Others see that year, and your age, only if you set your birthday again with `show_age` on.

Whoever runs the bot can read everything it stores.

## How to delete your birthday

Run `/birthday forget` in any server the bot shares with you. It deletes your birthday, your `show_age` choice and the list of servers where you set it, and removes you from every server's list, not only the one where you run it. Only you see the reply.

It does not delete Discord messages. Earlier versions let members set a birthday by mentioning the bot; such a message and the bot's reply stay in the channel until someone deletes them.

If you no longer share a server with the bot, you cannot run `/birthday forget`. Ask whoever runs the bot to delete your data.

## When you leave a server

Leaving a server deletes nothing. You stay on that server's list, and `/birthday list`, `/birthday upcoming` and the monthly post there still show your birthday, until you run `/birthday forget`.

## When the bot leaves a server

When the bot is removed from a server, or the server is deleted, the bot deletes that server's list. If that was the only server where you set your birthday, it also deletes your birthday, your `show_age` choice and your list of servers, because no server can see them any more. Other servers where you set it still see it.

This cleanup runs once, when Discord tells the running bot about the removal, and it is not retried. If the bot is not running at that moment, Discord may never tell it; if the bot cannot reach its database, the cleanup stops. Whatever it did not delete stays until you run `/birthday forget` or whoever runs the bot deletes it.

## Where the data is kept

Everything the bot stores is kept in the Redis database of whoever runs the bot, at the address in its `REDIS_URL` setting. This repository holds only the code, not any data.

When something goes wrong, the bot prints an error to its log. Some of these messages include user IDs, server IDs or names, and sometimes a stored birthday. The log is kept wherever the person running the bot keeps it.
