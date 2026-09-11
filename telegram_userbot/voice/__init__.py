"""Voice-chat relay services for hosted Telegram accounts.

The manager is imported by the hosted-client lifecycle only. Keeping this
package initializer lightweight lets the PCM unit tests run without MongoDB
client dependencies.
"""